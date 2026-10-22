#!/usr/bin/env python3
"""Nightly sync of Garmin wellness + activity data to local files for Claude to read.

Uses Garmin's Personal API (developer.garmin.com/gc-developer-program/personal/).
Token flow: Personal API key -> short-lived OAuth2 bearer token -> data endpoints.
No third-party Garmin client library is used; only the stdlib + `requests`.
"""

import argparse
import base64
import csv
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
WELLNESS_CSV = ROOT / "wellness.csv"
ACTIVITIES_DIR = ROOT / "activities"
WELLNESS_RAW_DIR = ROOT / "wellness_raw"
LOG_FILE = ROOT / "fetch.log"
TOKEN_CACHE_FILE = ROOT / ".token_cache.json"

TOKEN_URL = "https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token"
API_BASE = "https://personal.connectapi.garmin.com"

CSV_FIELDS = ["date", "resting_hr", "hrv_last_night_avg", "sleep_score", "sleep_hours"]

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("garmin-sync")


def load_api_key():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "GARMIN_API_KEY":
                return value.strip()
    import os
    return os.environ.get("GARMIN_API_KEY")


def jwt_exp(token):
    """Decode (without verifying) the `exp` claim from a JWT's payload segment."""
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["exp"]


def obtain_token(api_key):
    """Exchange the long-lived Personal API key for a short-lived bearer token.

    The token endpoint returns the raw JWT as plain text (not JSON-wrapped).
    Tokens are valid ~24h (per the token's own `exp` claim); cached to disk so
    repeated cron runs within that window don't hit the token endpoint every time.
    """
    if TOKEN_CACHE_FILE.exists():
        try:
            cached = json.loads(TOKEN_CACHE_FILE.read_text())
            if cached.get("api_key_fingerprint") == api_key[-8:] and cached.get("expires_at", 0) > time.time() + 300:
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass

    resp = requests.post(TOKEN_URL, headers={"Authorization": f"ApiKey {api_key}"}, timeout=30)
    resp.raise_for_status()
    access_token = resp.text.strip()

    TOKEN_CACHE_FILE.write_text(json.dumps({
        "access_token": access_token,
        "expires_at": jwt_exp(access_token),
        "api_key_fingerprint": api_key[-8:],
    }))
    TOKEN_CACHE_FILE.chmod(0o600)
    return access_token


def api_get(token, path, params=None):
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def first(d, *keys, default=None):
    """Return the first present, non-null key from a dict of unconfirmed schema."""
    for key in keys:
        if isinstance(d, dict) and d.get(key) is not None:
            return d[key]
    return default


def fetch_wellness_day(token, day):
    iso = day.isoformat()
    raw = {}

    for name, path in [
        ("heart_rate", "/wellness-service/wellness/dailyHeartRate"),
        ("hrv", f"/hrv-service/hrv/{iso}"),
        ("sleep", "/sleep-service/sleep/dailySleep"),
    ]:
        try:
            params = None if name == "hrv" else {"date": iso}
            raw[name] = api_get(token, path, params=params)
        except requests.HTTPError as e:
            log.warning("%s fetch failed for %s: %s", name, iso, e)
            raw[name] = None

    WELLNESS_RAW_DIR.mkdir(exist_ok=True)
    (WELLNESS_RAW_DIR / f"{iso}.json").write_text(json.dumps(raw, indent=2, default=str))

    hr = raw["heart_rate"] or {}
    hrv_summary = (raw["hrv"] or {}).get("hrvSummary") or {}
    sleep = raw["sleep"] or {}
    sleep_overall = ((sleep.get("sleepScores") or {}).get("overall")) or {}

    resting_hr = first(hr, "restingHeartRate", "restingHeartRateInBeatsPerMinute")
    hrv_avg = first(hrv_summary, "lastNightAvg", "weeklyAvg", "lastNight5MinHigh")
    sleep_score = first(sleep_overall, "value") or first(sleep, "sleepScore", "overallSleepScore")
    sleep_seconds = first(sleep, "sleepTimeSeconds", "totalSleepSeconds", "sleepDurationInSeconds")
    sleep_hours = round(sleep_seconds / 3600, 2) if sleep_seconds else None

    missing = [name for name, val in [
        ("resting_hr", resting_hr), ("hrv", hrv_avg),
        ("sleep_score", sleep_score), ("sleep_hours", sleep_hours),
    ] if val is None]
    if missing:
        log.warning(
            "%s: could not extract %s from API response - inspect wellness_raw/%s.json "
            "and add the real field name(s) to the `first(...)` calls in fetch_training_data.py",
            iso, ", ".join(missing), iso,
        )

    return {
        "date": iso,
        "resting_hr": resting_hr,
        "hrv_last_night_avg": hrv_avg,
        "sleep_score": sleep_score,
        "sleep_hours": sleep_hours,
    }


def fetch_activities_day(token, day):
    iso = day.isoformat()
    try:
        activities = api_get(
            token,
            "/activitylist-service/activities/search/activities",
            params={"startDate": iso, "endDate": iso, "limit": 50},
        )
    except requests.HTTPError as e:
        log.warning("activities fetch failed for %s: %s", iso, e)
        activities = []

    ACTIVITIES_DIR.mkdir(exist_ok=True)
    (ACTIVITIES_DIR / f"{iso}.json").write_text(json.dumps(activities, indent=2, default=str))
    return len(activities) if isinstance(activities, list) else 0


def upsert_wellness_row(row):
    rows = {}
    if WELLNESS_CSV.exists():
        with WELLNESS_CSV.open(newline="") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = r
    rows[row["date"]] = row
    with WELLNESS_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for d in sorted(rows):
            writer.writerow(rows[d])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=2,
        help="How many trailing days to fetch, ending today (default: 2 = yesterday+today)",
    )
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        sys.exit("GARMIN_API_KEY not set. Put it in .env (GARMIN_API_KEY=...) next to this script.")

    try:
        token = obtain_token(api_key)
    except requests.HTTPError as e:
        log.error("token exchange failed: %s", e)
        sys.exit(f"Could not obtain an access token: {e}")

    today = date.today()
    days = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    for day in days:
        row = fetch_wellness_day(token, day)
        upsert_wellness_row(row)
        n_activities = fetch_activities_day(token, day)
        log.info(
            "%s: resting_hr=%s hrv=%s sleep_score=%s sleep_hours=%s activities=%d",
            row["date"], row["resting_hr"], row["hrv_last_night_avg"],
            row["sleep_score"], row["sleep_hours"], n_activities,
        )

    print(f"Synced {len(days)} day(s) -> {WELLNESS_CSV.name}, activities/, wellness_raw/")


if __name__ == "__main__":
    main()
