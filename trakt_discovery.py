#!/usr/bin/env python3
"""
Trakt Content Discovery for cascade-tv
- Discovers trending, popular, anticipated, and recommended content via Trakt
- Requests discovered content through Jellyseerr (TV → Sonarr, Movies → Radarr)
- TV shows request Season 1 only — cascade-tv's E01-preview logic handles the rest
- OAuth device code flow for Trakt authentication
"""

import requests
import json
import time
import os
import sys
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y/%m/%d %H:%M:%S"
)
log = logging.getLogger("trakt-discovery")

# ============================================================
# CONFIGURATION
# ============================================================
def get_float_env(key, default):
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        log.error(f"Invalid {key}='{os.getenv(key)}', must be a number. Using default: {default}")
        return default

def get_int_env(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        log.error(f"Invalid {key}='{os.getenv(key)}', must be an integer. Using default: {default}")
        return default

# Trakt API
TRAKT_CLIENT_ID = os.getenv("TRAKT_CLIENT_ID", "")
TRAKT_CLIENT_SECRET = os.getenv("TRAKT_CLIENT_SECRET", "")
TRAKT_BASE_URL = "https://api.trakt.tv"

# Discovery settings
TRAKT_DISCOVERY_ENABLED = os.getenv("TRAKT_DISCOVERY_ENABLED", "false").lower() == "true"
TRAKT_DISCOVERY_INTERVAL_HOURS = get_int_env("TRAKT_DISCOVERY_INTERVAL_HOURS", 6)
TRAKT_DISCOVER_SHOWS = os.getenv("TRAKT_DISCOVER_SHOWS", "true").lower() == "true"
TRAKT_DISCOVER_MOVIES = os.getenv("TRAKT_DISCOVER_MOVIES", "true").lower() == "true"
TRAKT_LISTS = [s.strip() for s in os.getenv("TRAKT_LISTS", "trending,popular,anticipated").split(",") if s.strip()]
TRAKT_MIN_RATING = get_float_env("TRAKT_MIN_RATING", 7.0)
TRAKT_MIN_VOTES = get_int_env("TRAKT_MIN_VOTES", 100)
TRAKT_YEARS = os.getenv("TRAKT_YEARS", "").strip()
TRAKT_GENRES = os.getenv("TRAKT_GENRES", "").strip()
TRAKT_LANGUAGES = os.getenv("TRAKT_LANGUAGES", "en").strip()
TRAKT_MAX_REQUESTS_PER_CYCLE = get_int_env("TRAKT_MAX_REQUESTS_PER_CYCLE", 10)
TRAKT_ITEMS_PER_LIST = get_int_env("TRAKT_ITEMS_PER_LIST", 20)

# Jellyseerr
JELLYSEERR_URL = os.getenv("JELLYSEERR_URL", "")
JELLYSEERR_API_KEY = os.getenv("JELLYSEERR_API_KEY", "")
JELLYSEERR_HEADERS = {"X-Api-Key": JELLYSEERR_API_KEY, "Content-Type": "application/json"}
JELLYSEERR_USER_ID = get_int_env("JELLYSEERR_USER_ID", 0)  # Jellyseerr user ID to attribute requests to

# Database
DB_PATH = os.getenv("DB_PATH", "/data/media_automation.db")

# Dry run
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ============================================================
# API HELPERS
# ============================================================
def _api_request_with_retry(method, url, headers, max_retries=3, **kwargs):
    """Make API request with retry logic for transient failures."""
    for attempt in range(max_retries):
        try:
            resp = method(url, headers=headers, timeout=30, **kwargs)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', 60))
                log.warning(f"Rate limited, waiting {retry_after}s before retry")
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    log.warning(f"Server error {resp.status_code}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue

            resp.raise_for_status()

            try:
                return resp.json()
            except json.JSONDecodeError:
                log.error(f"Invalid JSON response from {url}: {resp.text[:200]}")
                return None

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                log.warning(f"Request timeout, retrying ({attempt + 1}/{max_retries})")
                continue
            log.error(f"Request timed out after {max_retries} attempts: {url}")
            raise
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                log.warning(f"Connection error, retrying in {wait_time}s ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            log.error(f"Connection failed after {max_retries} attempts: {url}")
            raise
        except requests.exceptions.RequestException as e:
            log.error(f"API request failed: {url} - {e}")
            raise

    return None


def trakt_get(endpoint, params=None, auth_required=False, conn=None):
    """GET request to Trakt API."""
    headers = {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
    }
    if auth_required and conn:
        token = get_valid_token(conn)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            log.warning("Auth required but no valid token available")
            return None
    return _api_request_with_retry(requests.get, f"{TRAKT_BASE_URL}{endpoint}", headers, params=params)


def trakt_post(endpoint, data=None, headers_override=None):
    """POST request to Trakt API."""
    headers = headers_override or {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": TRAKT_CLIENT_ID,
    }
    return _api_request_with_retry(requests.post, f"{TRAKT_BASE_URL}{endpoint}", headers, json=data)


def jellyseerr_get(endpoint):
    """GET request to Jellyseerr API."""
    return _api_request_with_retry(requests.get, f"{JELLYSEERR_URL}/api/v1{endpoint}", JELLYSEERR_HEADERS)


def jellyseerr_post(endpoint, data):
    """POST request to Jellyseerr API."""
    return _api_request_with_retry(requests.post, f"{JELLYSEERR_URL}/api/v1{endpoint}", JELLYSEERR_HEADERS, json=data)


# ============================================================
# DATABASE
# ============================================================
def init_db():
    """Initialize SQLite database with Trakt discovery tables."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")

            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS trakt_tokens (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS trakt_discovered (
                    media_type TEXT NOT NULL,
                    trakt_id INTEGER NOT NULL,
                    tmdb_id INTEGER,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    rating REAL,
                    PRIMARY KEY (media_type, trakt_id, source)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS trakt_request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    requested_at TEXT NOT NULL
                )
            """)
            conn.commit()
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                wait_time = 0.5 * (attempt + 1)
                log.warning(f"Database locked, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                log.error(f"Database initialization failed: {e}")
                raise


# ============================================================
# OAUTH - DEVICE CODE FLOW
# ============================================================
def save_tokens(conn, token_data):
    """Save OAuth tokens to database."""
    now = datetime.now(timezone.utc).isoformat()
    expires_at = datetime.fromtimestamp(
        token_data["created_at"] + token_data["expires_in"], tz=timezone.utc
    ).isoformat()

    conn.execute("""
        INSERT OR REPLACE INTO trakt_tokens (id, access_token, refresh_token, expires_at, created_at)
        VALUES (1, ?, ?, ?, ?)
    """, (token_data["access_token"], token_data["refresh_token"], expires_at, now))
    conn.commit()
    log.info(f"Tokens saved (expires: {expires_at})")


def load_tokens(conn):
    """Load OAuth tokens from database. Returns dict or None."""
    row = conn.execute("SELECT access_token, refresh_token, expires_at FROM trakt_tokens WHERE id = 1").fetchone()
    if row:
        return {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}
    return None


def refresh_access_token(conn, refresh_token):
    """Refresh the Trakt access token."""
    log.info("Refreshing Trakt access token...")
    data = {
        "refresh_token": refresh_token,
        "client_id": TRAKT_CLIENT_ID,
        "client_secret": TRAKT_CLIENT_SECRET,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "refresh_token",
    }
    try:
        resp = requests.post(f"{TRAKT_BASE_URL}/oauth/token", json=data, timeout=30)
        resp.raise_for_status()
        token_data = resp.json()
        save_tokens(conn, token_data)
        log.info("Token refreshed successfully")
        return token_data["access_token"]
    except requests.exceptions.RequestException as e:
        log.error(f"Token refresh failed: {e}")
        return None


def get_valid_token(conn):
    """Get a valid access token, refreshing if needed."""
    tokens = load_tokens(conn)
    if not tokens:
        log.error("No Trakt tokens found. Run: python trakt_discovery.py auth")
        return None

    expires_at = datetime.fromisoformat(tokens["expires_at"])
    now = datetime.now(timezone.utc)

    # Refresh if within 7 days of expiry
    if now >= expires_at - timedelta(days=7):
        refreshed = refresh_access_token(conn, tokens["refresh_token"])
        if refreshed:
            return refreshed
        if now >= expires_at:
            log.error("Token expired and refresh failed. Run: python trakt_discovery.py auth")
            return None

    return tokens["access_token"]


def init_device_auth(conn):
    """Start Trakt device code OAuth flow."""
    if not TRAKT_CLIENT_ID:
        log.error("TRAKT_CLIENT_ID not set. Create an app at https://trakt.tv/oauth/applications")
        return False

    log.info("Starting Trakt device code authentication...")
    data = {"client_id": TRAKT_CLIENT_ID}
    try:
        resp = requests.post(f"{TRAKT_BASE_URL}/oauth/device/code", json=data, timeout=30)
        resp.raise_for_status()
        device_data = resp.json()
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to get device code: {e}")
        return False

    user_code = device_data["user_code"]
    verification_url = device_data["verification_url"]
    device_code = device_data["device_code"]
    interval = device_data.get("interval", 5)
    expires_in = device_data["expires_in"]

    print(f"\n{'='*50}")
    print(f"  Go to: {verification_url}")
    print(f"  Enter code: {user_code}")
    print(f"  (Code expires in {expires_in // 60} minutes)")
    print(f"{'='*50}\n")

    return poll_device_token(conn, device_code, interval, expires_in)


def poll_device_token(conn, device_code, interval, expires_in):
    """Poll for device token approval."""
    data = {
        "code": device_code,
        "client_id": TRAKT_CLIENT_ID,
        "client_secret": TRAKT_CLIENT_SECRET,
    }
    deadline = time.time() + expires_in

    while time.time() < deadline:
        time.sleep(interval)
        try:
            resp = requests.post(f"{TRAKT_BASE_URL}/oauth/device/token", json=data, timeout=30)

            if resp.status_code == 200:
                token_data = resp.json()
                save_tokens(conn, token_data)
                log.info("Authentication successful!")
                return True
            elif resp.status_code == 400:
                # Pending - user hasn't approved yet
                continue
            elif resp.status_code == 404:
                log.error("Invalid device code")
                return False
            elif resp.status_code == 409:
                log.error("Code already approved")
                return False
            elif resp.status_code == 410:
                log.error("Code expired")
                return False
            elif resp.status_code == 418:
                log.error("User denied the request")
                return False
            elif resp.status_code == 429:
                # Slow down
                interval += 1
                continue
        except requests.exceptions.RequestException as e:
            log.warning(f"Poll error: {e}")
            continue

    log.error("Device code expired before approval")
    return False


# ============================================================
# DISCOVERY LOGIC
# ============================================================
def fetch_list(conn, list_type, media_type):
    """Fetch a Trakt list. Returns list of items with extended info."""
    # Build endpoint
    # 'recommended' and 'watchlist' require auth
    auth_required = list_type in ("recommended", "watchlist")

    if list_type == "watchlist":
        endpoint = f"/users/me/watchlist/{media_type}s"
        params = {"extended": "full"}
    elif list_type == "recommended":
        endpoint = f"/recommendations/{media_type}s"
        params = {"extended": "full"}
    else:
        # trending, popular, anticipated
        endpoint = f"/{media_type}s/{list_type}"
        params = {"extended": "full", "limit": str(TRAKT_ITEMS_PER_LIST)}

    # Add filters as query params
    if TRAKT_GENRES:
        params["genres"] = TRAKT_GENRES
    if TRAKT_YEARS:
        params["years"] = TRAKT_YEARS
    if TRAKT_LANGUAGES:
        params["languages"] = TRAKT_LANGUAGES

    items = trakt_get(endpoint, params=params, auth_required=auth_required, conn=conn)
    if items is None:
        log.warning(f"Failed to fetch {list_type} {media_type}s")
        return []

    if not isinstance(items, list):
        log.warning(f"Unexpected response for {list_type} {media_type}s: {type(items)}")
        return []

    return items


def extract_item_info(item, media_type, list_type):
    """Extract standardized info from a Trakt list item."""
    # Trakt wraps items differently depending on list type
    # trending: {"watchers": N, "show": {...}} or {"watchers": N, "movie": {...}}
    # popular: [{"title": ..., "ids": {...}}] (direct)
    # anticipated: {"list_count": N, "show": {...}} or {"list_count": N, "movie": {...}}
    # recommended: [{"title": ..., "ids": {...}}] (direct)
    # watchlist: {"show": {...}} or {"movie": {...}}

    media_key = "show" if media_type == "show" else "movie"

    if media_key in item:
        media = item[media_key]
    elif "title" in item and "ids" in item:
        # Direct item (popular, recommended)
        media = item
    else:
        return None

    ids = media.get("ids", {})
    return {
        "trakt_id": ids.get("trakt"),
        "tmdb_id": ids.get("tmdb"),
        "title": media.get("title", "Unknown"),
        "year": media.get("year"),
        "rating": media.get("rating", 0),
        "votes": media.get("votes", 0),
    }


def is_already_discovered(conn, media_type, trakt_id):
    """Check if an item has already been discovered (any source)."""
    row = conn.execute(
        "SELECT 1 FROM trakt_discovered WHERE media_type = ? AND trakt_id = ?",
        (media_type, trakt_id)
    ).fetchone()
    return row is not None


def check_jellyseerr_status(media_type, tmdb_id):
    """Check if media already exists or is requested in Jellyseerr. Returns True if available/requested."""
    endpoint = f"/{'tv' if media_type == 'show' else 'movie'}/{tmdb_id}"
    try:
        data = jellyseerr_get(endpoint)
        if data is None:
            return False
        # Jellyseerr mediaInfo.status: 1=unknown, 2=pending, 3=processing, 4=partially_available, 5=available
        media_info = data.get("mediaInfo")
        if media_info:
            status = media_info.get("status", 1)
            if status >= 2:
                return True
        return False
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return False
        log.warning(f"Jellyseerr status check failed for {media_type} TMDB:{tmdb_id}: {e}")
        return False
    except Exception as e:
        log.warning(f"Jellyseerr status check error for {media_type} TMDB:{tmdb_id}: {e}")
        return False


def request_via_jellyseerr(media_type, tmdb_id, title):
    """Request content through Jellyseerr. Returns True if successful."""
    if media_type == "show":
        payload = {"mediaType": "tv", "mediaId": tmdb_id, "seasons": [1]}
    else:
        payload = {"mediaType": "movie", "mediaId": tmdb_id}

    if JELLYSEERR_USER_ID:
        payload["userId"] = JELLYSEERR_USER_ID

    if DRY_RUN:
        log.info(f"[DRY RUN] Would request {media_type} '{title}' (TMDB:{tmdb_id}) via Jellyseerr")
        return True

    try:
        result = jellyseerr_post("/request", payload)
        if result:
            season_info = " (Season 1)" if media_type == "show" else ""
            log.info(f"Requested {media_type} '{title}'{season_info} via Jellyseerr (TMDB:{tmdb_id})")
            return True
        return False
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 409:
            log.info(f"Already requested: '{title}' (TMDB:{tmdb_id})")
            return False
        log.error(f"Jellyseerr request failed for '{title}': {e}")
        return False
    except Exception as e:
        log.error(f"Jellyseerr request error for '{title}': {e}")
        return False


def record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, action, rating):
    """Record a discovered item in the database."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT OR REPLACE INTO trakt_discovered (media_type, trakt_id, tmdb_id, title, source, discovered_at, action, rating)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (media_type, trakt_id, tmdb_id, title, source, now, action, rating))
    conn.commit()


def record_request(conn, media_type, tmdb_id, title, source):
    """Record a successful request in the log."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO trakt_request_log (media_type, tmdb_id, title, source, requested_at)
        VALUES (?, ?, ?, ?, ?)
    """, (media_type, tmdb_id, title, source, now))
    conn.commit()


def fetch_watched_ids(conn):
    """Fetch Trakt IDs of all watched shows and movies. Returns dict of sets keyed by media type."""
    watched = {"show": set(), "movie": set()}
    for media_type in ("show", "movie"):
        endpoint = f"/users/me/watched/{media_type}s"
        items = trakt_get(endpoint, auth_required=True, conn=conn)
        if not items or not isinstance(items, list):
            log.warning(f"Could not fetch watched {media_type}s from Trakt")
            continue
        for item in items:
            media = item.get(media_type, {})
            trakt_id = media.get("ids", {}).get("trakt")
            if trakt_id:
                watched[media_type].add(trakt_id)
        log.info(f"Loaded {len(watched[media_type])} watched {media_type}s from Trakt history")
    return watched


def process_discovered_item(conn, item, media_type, source, request_count, max_requests, watched_ids=None):
    """Process a single discovered item through the filter pipeline.
    Returns updated request_count."""
    info = extract_item_info(item, media_type, source)
    if not info or not info["trakt_id"]:
        return request_count

    title = info["title"]
    trakt_id = info["trakt_id"]
    tmdb_id = info["tmdb_id"]
    rating = info["rating"]
    votes = info["votes"]

    # Skip if no TMDB ID
    if not tmdb_id:
        log.debug(f"Skipping '{title}' — no TMDB ID")
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_no_tmdb", rating)
        return request_count

    # Skip if already discovered
    if is_already_discovered(conn, media_type, trakt_id):
        return request_count

    # Skip if already watched on Trakt
    if watched_ids and trakt_id in watched_ids.get(media_type, set()):
        log.debug(f"Skipping '{title}' — already watched on Trakt")
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_watched", rating)
        return request_count

    # Skip if below rating threshold
    if rating and rating < TRAKT_MIN_RATING:
        log.debug(f"Skipping '{title}' — rating {rating:.1f} < {TRAKT_MIN_RATING}")
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_rating", rating)
        return request_count

    # Skip if below vote threshold
    if votes and votes < TRAKT_MIN_VOTES:
        log.debug(f"Skipping '{title}' — {votes} votes < {TRAKT_MIN_VOTES}")
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_rating", rating)
        return request_count

    # Check if already in Jellyseerr
    if check_jellyseerr_status(media_type, tmdb_id):
        log.debug(f"Skipping '{title}' — already in Jellyseerr")
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_exists", rating)
        return request_count

    # Respect request limit
    if request_count >= max_requests:
        log.info(f"Request limit reached ({max_requests}), stopping")
        return request_count

    # Request via Jellyseerr
    if request_via_jellyseerr(media_type, tmdb_id, title):
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "requested", rating)
        record_request(conn, media_type, tmdb_id, title, source)
        request_count += 1
        year_str = f" ({info['year']})" if info.get("year") else ""
        rating_str = f" [{rating:.1f}]" if rating else ""
        log.info(f"[{request_count}/{max_requests}] Discovered & requested: '{title}'{year_str}{rating_str} from {source}")
    else:
        record_discovered(conn, media_type, trakt_id, tmdb_id, title, source, "skipped_exists", rating)

    return request_count


def discover_content(conn):
    """Main discovery orchestrator. Loops through configured lists and media types."""
    if not TRAKT_CLIENT_ID:
        log.error("TRAKT_CLIENT_ID not set. Cannot discover content.")
        return

    if not JELLYSEERR_URL or not JELLYSEERR_API_KEY:
        log.error("JELLYSEERR_URL and JELLYSEERR_API_KEY required for content requests.")
        return

    media_types = []
    if TRAKT_DISCOVER_SHOWS:
        media_types.append("show")
    if TRAKT_DISCOVER_MOVIES:
        media_types.append("movie")

    if not media_types:
        log.warning("Neither shows nor movies enabled for discovery")
        return

    log.info(f"Starting Trakt discovery cycle (lists: {TRAKT_LISTS}, types: {media_types}, max: {TRAKT_MAX_REQUESTS_PER_CYCLE})")
    if DRY_RUN:
        log.info("[DRY RUN] No actual requests will be made")

    # Fetch watch history to skip already-watched content
    watched_ids = fetch_watched_ids(conn)

    request_count = 0

    for list_type in TRAKT_LISTS:
        for media_type in media_types:
            if request_count >= TRAKT_MAX_REQUESTS_PER_CYCLE:
                log.info(f"Request limit reached ({TRAKT_MAX_REQUESTS_PER_CYCLE})")
                break

            log.info(f"Fetching {list_type} {media_type}s...")
            items = fetch_list(conn, list_type, media_type)
            log.info(f"  Found {len(items)} items")

            for item in items:
                if request_count >= TRAKT_MAX_REQUESTS_PER_CYCLE:
                    break
                try:
                    request_count = process_discovered_item(
                        conn, item, media_type, list_type,
                        request_count, TRAKT_MAX_REQUESTS_PER_CYCLE,
                        watched_ids=watched_ids
                    )
                except Exception as e:
                    log.error(f"Error processing item: {e}", exc_info=True)
                    continue

        if request_count >= TRAKT_MAX_REQUESTS_PER_CYCLE:
            break

    log.info(f"Discovery cycle complete: {request_count} new requests")


# ============================================================
# CLI COMMANDS
# ============================================================
def cmd_auth(conn):
    """Run OAuth device code flow."""
    return init_device_auth(conn)


def cmd_reauth(conn):
    """Clear existing token and re-run OAuth flow."""
    conn.execute("DELETE FROM trakt_tokens")
    conn.commit()
    log.info("Cleared existing tokens")
    return init_device_auth(conn)


def cmd_status(conn):
    """Show token validity, discovery stats, recent requests."""
    # Token status
    tokens = load_tokens(conn)
    if tokens:
        expires_at = datetime.fromisoformat(tokens["expires_at"])
        now = datetime.now(timezone.utc)
        if now < expires_at:
            days_left = (expires_at - now).days
            print(f"Token: Valid ({days_left} days until expiry)")
        else:
            print("Token: EXPIRED — run 'auth' to re-authenticate")
    else:
        print("Token: Not configured — run 'auth' to authenticate")

    # Discovery stats
    row = conn.execute("SELECT COUNT(*) FROM trakt_discovered").fetchone()
    total_discovered = row[0] if row else 0

    row = conn.execute("SELECT COUNT(*) FROM trakt_discovered WHERE action = 'requested'").fetchone()
    total_requested = row[0] if row else 0

    row = conn.execute("SELECT COUNT(*) FROM trakt_request_log").fetchone()
    total_logged = row[0] if row else 0

    print(f"\nDiscovery Stats:")
    print(f"  Total discovered: {total_discovered}")
    print(f"  Total requested:  {total_requested}")
    print(f"  Request log entries: {total_logged}")

    # Breakdown by action
    rows = conn.execute(
        "SELECT action, COUNT(*) FROM trakt_discovered GROUP BY action ORDER BY COUNT(*) DESC"
    ).fetchall()
    if rows:
        print(f"\n  By action:")
        for action, count in rows:
            print(f"    {action}: {count}")

    # Recent requests
    rows = conn.execute(
        "SELECT media_type, title, source, requested_at FROM trakt_request_log ORDER BY requested_at DESC LIMIT 10"
    ).fetchall()
    if rows:
        print(f"\nRecent Requests:")
        for media_type, title, source, requested_at in rows:
            ts = requested_at[:19].replace("T", " ")
            print(f"  [{media_type}] {title} (from {source}) — {ts}")


def cmd_reset(conn):
    """Clear discovered items table for a fresh start."""
    conn.execute("DELETE FROM trakt_discovered")
    conn.commit()
    row = conn.execute("SELECT COUNT(*) FROM trakt_request_log").fetchone()
    log.info(f"Cleared discovered items table. Request log retained ({row[0]} entries).")


def cmd_discover(conn):
    """Run one discovery cycle."""
    discover_content(conn)


# ============================================================
# MAIN
# ============================================================
def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "discover"

    conn = init_db()
    try:
        if command == "auth":
            success = cmd_auth(conn)
            sys.exit(0 if success else 1)
        elif command == "reauth":
            success = cmd_reauth(conn)
            sys.exit(0 if success else 1)
        elif command == "status":
            cmd_status(conn)
        elif command == "reset":
            cmd_reset(conn)
        elif command == "discover":
            cmd_discover(conn)
        else:
            print(f"Unknown command: {command}")
            print("Usage: python trakt_discovery.py [auth|reauth|discover|status|reset]")
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
