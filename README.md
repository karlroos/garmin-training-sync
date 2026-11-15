# garmin-training-sync

A small script that syncs your own Garmin Connect wellness and activity data
to local files, using Garmin's official Personal API (not a scraped/unofficial
client).

It pulls, for each of the last N days (default: yesterday + today):

- Daily summaries for heart rate (resting/min/max/7-day avg), HRV (last-night
  avg, weekly avg, 5-min high, baseline status), sleep (score, duration, stage
  breakdown, respiration, sleep-window stress, sleep avg HR), body battery
  (charged/drained points, highest/lowest/start/end level), steps (total,
  distance, goal), and all-day stress (overall score, minutes spent in
  rest/low/medium/high stress) → upserted into `wellness.csv`, one row per day.
- The day's activity list (runs, rides, etc.) → written to `activities/<date>.json`.
- The full raw API responses (heart rate, HRV, sleep, body battery, steps,
  stress) → written to `wellness_raw/<date>.json`. This includes the actual
  time series behind the daily summaries — heart rate every ~2 minutes, HRV
  every ~5 minutes overnight, body battery through the day — in case you need
  finer granularity than one row per day, or a CSV field comes back blank and
  you need to dig into the raw payload.

Not pulled, because there's no working endpoint for it on this Personal API
(confirmed by testing, not just absence of docs): standalone respiration,
pulse ox, VO2max/fitness age, training readiness, race predictions, endurance
score, hill score, calories, floors climbed, and intensity minutes. All of
these only show up in Garmin's full account data export (Settings → Export
Your Data), not through this API.

Nothing here is specific to one athlete — point it at your own Garmin account
and it writes your own data to your own local files. No data is sent anywhere
except to Garmin's API to fetch it.

## Setup

1. Get a Garmin Personal API key from
   [connect.garmin.com/app/settings/personalApiToken](https://connect.garmin.com/app/settings/personalApiToken).
2. Create a `.env` file next to the script:
   ```
   GARMIN_API_KEY=your-key-here
   ```
3. Install the one dependency: `pip install requests`

## Usage

```bash
python3 fetch_training_data.py          # yesterday + today (default)
python3 fetch_training_data.py --days 10  # last 10 days, backfill
```

Output files (all gitignored, since they're personal data):

- `wellness.csv`
- `activities/<date>.json`
- `wellness_raw/<date>.json`
- `fetch.log` — run history; check this first if a day's data looks missing
  or wrong (the extraction logic in `fetch_wellness_day` uses best-guess field
  names since Garmin's Personal API response schema isn't fully documented —
  if a field comes back blank, the log tells you which key to add).
- `.token_cache.json` — the exchanged bearer token, cached so repeated runs
  within its ~24h validity window don't re-hit the token endpoint.

## Notes

- Garmin's Personal API exchanges your long-lived API key for a short-lived
  bearer token per the standard flow; no third-party Garmin client library is
  used, only the Python stdlib + `requests`.
- Re-running for a date you already have is safe — `wellness.csv` rows are
  upserted by date, and activity/raw files are overwritten.
- The script forces IPv4 for all requests. On a host with a broken/unreliable
  IPv6 route to Garmin's API, `requests` can pick the IPv6 address and hang
  indefinitely instead of failing over to IPv4 or timing out — the `timeout=`
  kwarg doesn't help since the hang happens during connection, before it
  applies. If you don't have this problem, it's harmless to leave in.
- Large backfills (`--days 20+`) can trip Garmin's rate limit (HTTP 429).
  `api_get` retries with backoff when that happens, and there's a small pacing
  delay between days. `wellness.csv` rows are also upserted by merging
  field-by-field rather than replacing the whole row, so a day that still fails
  after retries can't silently blank out previously-fetched good data for that
  date — it only ever adds or updates fields it successfully fetched.
