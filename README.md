# garmin-training-sync

A small script that syncs your own Garmin Connect wellness and activity data
to local files, using Garmin's official Personal API (not a scraped/unofficial
client).

It pulls, for each of the last N days (default: yesterday + today):

- Resting heart rate, overnight HRV, sleep score, and sleep duration →
  upserted into `wellness.csv`.
- The day's activity list (runs, rides, etc.) → written to `activities/<date>.json`.
- The full raw API responses for heart rate/HRV/sleep → written to
  `wellness_raw/<date>.json`, in case a field is missing from the CSV and you
  need to dig into the raw payload.

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
