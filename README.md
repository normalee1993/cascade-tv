# Media Automation for Unraid

Two-part automation system for a self-hosted media stack (Seerr + Sonarr/Radarr + Jellyfin + SABnzbd):

**Smart download management** — When someone requests a show in Seerr, only the requested season is fully downloaded. Every other season gets just Episode 1 as a preview. As people watch, the next season is automatically unlocked: at 75% through a season, the next downloads in the background. If someone plays a preview episode, the full season is immediately prioritised in SABnzbd so E02 is ready before E01 finishes.

**Automatic content discovery** — Connects to Trakt to find trending, popular, and personalised recommended content, then requests it through Seerr automatically. A configurable filtering pipeline (rating, votes, year, genre, show status, episode count) keeps the library focused. High-rated classics on your Trakt watchlist or recommendations can bypass year/status filters so nothing worth watching gets silently dropped.

## How It Works

### When someone requests a show in Seerr

1. Seerr tells Sonarr to add the series (Sonarr monitors ALL episodes by default)
2. This script intercepts the Sonarr webhook and immediately fixes the monitoring:
   - **Requested season(s)**: All episodes downloaded
   - **Every other season**: Only Episode 1 downloaded (as a preview)
3. Unwanted downloads already queued in SABnzbd are automatically cancelled

### As people watch

The script polls Jellyfin every 15 minutes and checks watch progress for each configured user. When someone watches **75% or more** of a season, the next season is automatically unlocked (all episodes monitored + search triggered). Downloads for the new season are set to **High priority** in SABnzbd.

### When someone starts playing a preview episode

Every 45 seconds, the script checks Jellyfin's active sessions. If a user starts playing **Episode 1** of a season that only has the preview downloaded, the full season is automatically unlocked and searched. Downloads are prioritized in SABnzbd: **E02 gets Force priority** (downloads immediately so it's ready when E01 finishes) and **E03+ get High priority**.

### "All seasons" requests

If someone requests every season of a show, the script treats it as: **Season 1 full + Episode 1 of the rest**. This prevents downloading 15 seasons of a show nobody has started watching yet.

## Trakt Content Discovery

Automatically discovers trending, popular, and recommended content via the Trakt API and requests it through Seerr. TV shows request only Season 1 — the core automation's E01-preview logic handles progressive unlocking as people watch.

### What it monitors
- **Trending** — Currently most-watched shows and movies
- **Popular** — All-time most popular
- **Anticipated** — Most anticipated upcoming releases
- **Recommended** — Personalized recommendations based on your Trakt watch history
- **Watchlist** — Your Trakt watchlist

### Filtering pipeline
Each discovered item goes through these filters before being requested:
1. **Already discovered** — Skips items seen in previous cycles
2. **Already watched on Trakt** — Skips your complete watch history (import your Netflix/Amazon/etc. history to Trakt for best results)
3. **Rating threshold** — Skips items below `TRAKT_MIN_RATING` (default: 7.0)
4. **Vote threshold** — Skips items with fewer than `TRAKT_MIN_VOTES` (default: 100)
5. **Year filter** — Skips items outside `TRAKT_YEARS` range (backup for API-level filter that some list types ignore)
6. **Genre exclusion** — Skips items matching `TRAKT_EXCLUDE_GENRES` (e.g., `animation,reality,talk-show`)
7. **TMDB filters** (optional, requires `TMDB_API_KEY`) — Episode count (`TRAKT_MAX_EPISODES`), show status (`TRAKT_ALLOWED_SHOW_STATUS`), content rating (`TRAKT_ALLOWED_RATINGS` / `TRAKT_EXCLUDE_RATINGS`)
8. **Already in Seerr** — Skips items already requested or available
9. **Request limit** — Stops after per-type limits (`TRAKT_MAX_SHOW_REQUESTS` / `TRAKT_MAX_MOVIE_REQUESTS`)

### Premium Content Bypass

High-rated content from personalised lists (`recommended`, `watchlist` by default) can bypass the year and show status filters. The original `TRAKT_MIN_RATING` floor still applies to everything — a show must clear 7.0 before bypass is even considered.

**Example:** Breaking Bad (2008, Ended, rating 9.3) on your recommended list passes through even with `TRAKT_YEARS=2020-2026` and `TRAKT_ALLOWED_SHOW_STATUS=Returning Series`, because its 9.3 rating clears the 8.0 bypass bar.

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAKT_PREMIUM_BYPASS_ENABLED` | `true` | Toggle the whole feature on/off |
| `TRAKT_PREMIUM_BYPASS_MIN_RATING` | `8.0` | Minimum rating to qualify for bypass (above the normal 7.0 floor) |
| `TRAKT_PREMIUM_BYPASS_LISTS` | `recommended,watchlist` | Comma-separated list sources that get the bypass |
| `TRAKT_PREMIUM_BYPASS_FILTERS` | `year,status` | Which filters are bypassable: `year`, `status`, or `year,status` |

### Setup
1. Create a Trakt API application at https://trakt.tv/oauth/applications
   - **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob`
   - **Permissions:** No special permissions needed (leave /checkin and /scrobble unchecked)
2. Add your Client ID and Client Secret to `.env`
3. Rebuild: `docker compose build && docker compose up -d`
4. Authenticate: `docker exec media-automation python -u /app/trakt_discovery.py auth`
5. Visit the URL displayed, enter the code
6. Set `TRAKT_DISCOVERY_ENABLED=true` in `.env` and restart

### Commands
```bash
# Authenticate with Trakt
docker exec media-automation python -u /app/trakt_discovery.py auth

# Check token and discovery stats
docker exec media-automation python -u /app/trakt_discovery.py status

# Dry run (see what would be requested without making changes)
docker exec -e DRY_RUN=true media-automation python -u /app/trakt_discovery.py discover

# Run discovery now
docker exec media-automation python -u /app/trakt_discovery.py discover

# Clear discovered items for a fresh start (keeps request log)
docker exec media-automation python -u /app/trakt_discovery.py reset

# Re-authenticate (clear tokens and start over)
docker exec media-automation python -u /app/trakt_discovery.py reauth
```

---

## Setup

### 1. Configure Sonarr Webhook

In Sonarr, go to **Settings > Connect > Add > Webhook**:
- **Name**: Media Automation
- **URL**: `http://<your-server-ip>:9191`
- **Events**: Check "On Series Add"

### 2. Get API Keys

| Service | Where to find it |
|---------|-----------------|
| Sonarr | Settings > General > API Key |
| Jellyfin | Dashboard > API Keys > Create |
| Seerr | Settings > General > API Key |
| SABnzbd | Config > General > API Key |
| Jellyfin User IDs | Dashboard > Users > click user > ID is in the URL |

### 3. Configure Environment Variables

Copy the example configuration file and edit it with your API keys and server details:

```bash
cp .env.example .env
nano .env  # or use your preferred editor
```

Fill in your actual values for the API keys and service URLs.

### 4. Build and Run

```bash
docker compose build && docker compose up -d
```

## Environment Variables

All configuration is done via the `.env` file. See `.env.example` for a template.

| Variable | Default | Description |
|----------|---------|-------------|
| `SONARR_URL` | Required | Sonarr server URL |
| `SONARR_API_KEY` | | Sonarr API key |
| `JELLYFIN_URL` | Required | Jellyfin server URL |
| `JELLYFIN_API_KEY` | | Jellyfin API key |
| `SEERR_URL` | Required | Seerr server URL |
| `SEERR_API_KEY` | | Seerr API key (required to detect which season was requested) |
| `SEERR_USER_ID` | | Seerr user ID to attribute Trakt discovery requests to (see below) |
| `JELLYFIN_USER_IDS` | | Comma-separated Jellyfin user IDs to monitor for watch progress |
| `WATCH_THRESHOLD` | `0.75` | How much of a season must be watched before the next unlocks (0.75 = 75%) |
| `RUN_INTERVAL_MINUTES` | `15` | How often the polling loop runs (in minutes) |
| `NEW_SERIES_LOOKBACK_HOURS` | `48` | How far back to look for newly added series |
| `DRY_RUN` | `false` | Set to `true` to log what would happen without making changes |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `RUN_CATCHUP_ON_START` | `false` | Process all existing series on container start |
| `WEBHOOK_PORT` | `9191` | Port for the webhook listener |
| `SABNZBD_URL` | Required | SABnzbd server URL |
| `SABNZBD_API_KEY` | | SABnzbd API key (required for download priority management) |
| `PLAYBACK_CHECK_INTERVAL` | `45` | How often to check for active playback (in seconds) |
| `TRAKT_CLIENT_ID` | | Trakt API client ID (from https://trakt.tv/oauth/applications) |
| `TRAKT_CLIENT_SECRET` | | Trakt API client secret |
| `TRAKT_DISCOVERY_ENABLED` | `false` | Enable/disable automated Trakt discovery loop |
| `TRAKT_DISCOVERY_INTERVAL_HOURS` | `6` | How often the discovery loop runs |
| `TRAKT_DISCOVER_SHOWS` | `true` | Discover TV shows |
| `TRAKT_DISCOVER_MOVIES` | `true` | Discover movies |
| `TRAKT_LISTS` | `recommended,watchlist,trending,popular,anticipated` | Which Trakt lists to check (processed in order; personalized lists first ensures they get priority) |
| `TRAKT_MIN_RATING` | `7.0` | Minimum Trakt rating to request |
| `TRAKT_MIN_VOTES` | `100` | Minimum vote count to request |
| `TRAKT_MAX_REQUESTS_PER_CYCLE` | `10` | Max new requests per discovery cycle (split evenly between shows/movies if per-type limits not set) |
| `TRAKT_MAX_SHOW_REQUESTS` | | Max show requests per cycle (overrides even split) |
| `TRAKT_MAX_MOVIE_REQUESTS` | | Max movie requests per cycle (overrides even split) |
| `TRAKT_ITEMS_PER_LIST` | `20` | How many items to fetch per list |
| `TRAKT_LANGUAGES` | `en` | Language filter |
| `TRAKT_GENRES` | | Genre inclusion filter (comma-separated, leave empty for all) |
| `TRAKT_EXCLUDE_GENRES` | | Genre exclusion filter (comma-separated, e.g., `animation,reality,talk-show`) |
| `TRAKT_YEARS` | | Year filter (e.g., `2020-2026`) — applied at both API and application level |
| `TMDB_API_KEY` | | TMDB API key (enables episode count, show status, and content rating filters) |
| `TRAKT_MAX_EPISODES` | `0` | Skip shows with more than this many episodes (0 = disabled, requires `TMDB_API_KEY`) |
| `TRAKT_ALLOWED_SHOW_STATUS` | | Only allow shows with these statuses (e.g., `Returning Series,In Production,Planned`) |
| `TRAKT_ALLOWED_RATINGS` | | Only allow these content ratings (e.g., `TV-14,TV-MA,PG-13,R`) |
| `TRAKT_EXCLUDE_RATINGS` | | Exclude these content ratings (e.g., `TV-Y,TV-Y7,G`) |
| `TRAKT_PREMIUM_BYPASS_ENABLED` | `true` | Allow high-rated content from configured lists to bypass year/status filters |
| `TRAKT_PREMIUM_BYPASS_MIN_RATING` | `8.0` | Minimum rating to qualify for bypass |
| `TRAKT_PREMIUM_BYPASS_LISTS` | `recommended,watchlist` | List sources eligible for bypass |
| `TRAKT_PREMIUM_BYPASS_FILTERS` | `year,status` | Which filters the bypass can override (`year`, `status`, or both) |

### Finding Your Seerr User ID

The `SEERR_USER_ID` setting controls which Seerr user Trakt discovery requests are attributed to. This matters if you use tools like Jellysweep that act on content based on who requested it.

**Option A — Seerr web UI:**
1. Go to **Settings → Users**
2. Click **Edit** on the user you want
3. The numeric ID is in the page URL: `.../settings/users/16/edit` → ID is `16`

**Option B — API:**
```bash
curl -s -H "X-Api-Key: YOUR_SEERR_API_KEY" "http://YOUR_SEERR_URL/api/v1/user" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for u in data.get('results', []):
    print(f'  ID: {u[\"id\"]}, Name: {u.get(\"displayName\", \"?\")}, Email: {u.get(\"email\", \"?\")}')
"
```

Set `SEERR_USER_ID` in your `.env` to the numeric ID of the desired user. If not set, requests are attributed to the API key owner (typically the admin account).

---

## CLI Commands

Run these from your server terminal:

### View Logs
```bash
docker logs -f media-automation
```

### List All Processed Series
```bash
docker exec media-automation python /app/media_automation.py list
```
Shows every series the script has processed, with Sonarr IDs and timestamps.

### Reprocess a Specific Series
```bash
docker exec media-automation python /app/media_automation.py reprocess <sonarr_id>
```
Clears the series from the database and reprocesses it. Use this if monitoring got set wrong or you want to re-run the logic.

**How to find the Sonarr ID:**
1. **From the list command:** Run `docker exec media-automation python /app/media_automation.py list` to see all processed series with their IDs
2. **From Sonarr UI:** Go to the series page in Sonarr and look at the URL - it will be `http://your-sonarr:8989/series/<series-name>`. Click on the series to open the detail page, then check the browser's address bar or network tab for the numeric ID
3. **From Sonarr API:** Visit `http://your-sonarr:8989/api/v3/series?apikey=<your-api-key>` and search for your series in the JSON response - the `id` field is the Sonarr ID

### Manually Trigger a Full Run
```bash
docker exec media-automation python /app/media_automation.py
```
Runs the full polling cycle immediately: checks for new series, checks watch progress, cleans up stale DB entries.

### Process a Single Series (Webhook Mode)
```bash
docker exec media-automation python /app/media_automation.py webhook <sonarr_id>
```
Processes a single series as if it just arrived via webhook. Queries Seerr for the requested season(s) and sets monitoring accordingly.

### Check Active Playback
```bash
docker exec media-automation python /app/media_automation.py playback
```
Checks Jellyfin for active playback sessions and unlocks/prioritizes seasons if a user is watching a preview E01.

### Run Catch-up for Existing Series
```bash
docker exec media-automation python /app/media_automation.py catchup
```
One-time processing for series that were already in Sonarr before this script was installed. Respects seasons that already have downloaded files.

### Dry Run
```bash
docker exec -e DRY_RUN=true media-automation python /app/media_automation.py
```
Logs what changes would be made without actually modifying anything.

### Show Help
```bash
docker exec media-automation python /app/media_automation.py help
```

## Rebuild After Code Changes

```bash
docker compose build && docker compose up -d
```

## How the Database Works

The script uses a SQLite database at `/data/media_automation.db` (persisted via Docker volume) to track:

- **processed_series**: Which series have already had their monitoring configured
- **unlocked_seasons**: Which seasons have been fully unlocked (either by initial request or by watch progress)
- **priority_boosts**: Which seasons have had their SABnzbd download priorities boosted (prevents re-boosting on every poll cycle)

The database auto-cleans entries for series that no longer exist in Sonarr (e.g., deleted by JellySweep). You never need to manually edit the database under normal operation.

## Troubleshooting

**Show downloaded all seasons instead of just the requested one**
- Check that `SEERR_API_KEY` is set. Without it, the script can't determine which season was requested and falls back to Season 1.
- Check logs for "Seerr: Found request for..." to verify the lookup worked.

**Webhook was skipped**
- Look for "Script already running, skipping" in the logs. This only happens for polling runs, not webhooks. Webhooks have their own queue and will wait for each other.
- The series will still be picked up on the next 15-minute poll cycle.

**Want to change what season is fully monitored**
- Use the `reprocess` command after adjusting the request in Seerr.

**Watch progress not triggering next season**
- Verify the user's Jellyfin ID is in `JELLYFIN_USER_IDS`.
- Check that the watch threshold has been met (default 75%).
- Run `docker exec media-automation python /app/media_automation.py` to trigger a manual check.
