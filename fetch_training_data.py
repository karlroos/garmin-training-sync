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
import socket
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import urllib3.util.connection as urllib3_cn

# This environment's IPv6 route to Garmin's API is unreliable (connections hang
# rather than fail fast), while IPv4 works fine. Force IPv4 so a bad IPv6 route
# doesn't silently stall every request for minutes/hours.
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
WELLNESS_CSV = ROOT / "wellness.csv"
ACTIVITIES_DIR = ROOT / "activities"
WELLNESS_RAW_DIR = ROOT / "wellness_raw"
LOG_FILE = ROOT / "fetch.log"
TOKEN_CACHE_FILE = ROOT / ".token_cache.json"

TOKEN_URL = "https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token"
API_BASE = "https://personal.connectapi.garmin.com"

CSV_FIELDS = [
    "date",
    "resting_hr", "hr_min", "hr_max", "hr_7day_avg",
    "hrv_last_night_avg", "hrv_weekly_avg", "hrv_5min_high", "hrv_status",
    "sleep_score", "sleep_hours", "deep_sleep_min", "light_sleep_min", "rem_sleep_min",
    "awake_min", "awake_count", "avg_respiration", "avg_sleep_stress", "sleep_avg_hr",
    "body_battery_charged", "body_battery_drained", "body_battery_highest",
    "body_battery_lowest", "body_battery_start", "body_battery_end",
    "steps", "step_goal",
    "stress_overall", "stress_rest_min", "stress_low_min", "stress_medium_min", "stress_high_min",
]

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


def api_get(token, path, params=None, max_retries=4):
    """GET with retry/backoff on 429 (rate limit) - a multi-day backfill firing
    many requests back-to-back can trip Garmin's rate limiter mid-run otherwise,
    which previously surfaced as a silent all-fields-missing row for that day.
    """
    for attempt in range(max_retries + 1):
        resp = requests.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 429 and attempt < max_retries:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            time.sleep(wait)
            continue
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

    for name, path, params in [
        ("heart_rate", "/wellness-service/wellness/dailyHeartRate", {"date": iso}),
        ("hrv", f"/hrv-service/hrv/{iso}", None),
        ("sleep", "/sleep-service/sleep/dailySleep", {"date": iso}),
        # Body battery, steps, and all-day stress share the same range-query shape:
        # no per-date path, so a single-day report just repeats the date as both
        # the start and end of the range.
        ("body_battery", "/wellness-service/wellness/bodyBattery/reports/daily",
         {"startDate": iso, "endDate": iso}),
        ("steps", f"/usersummary-service/stats/steps/daily/{iso}/{iso}", None),
        ("stress", f"/usersummary-service/stats/stress/daily/{iso}/{iso}", None),
    ]:
        try:
            raw[name] = api_get(token, path, params=params)
        except requests.HTTPError as e:
            log.warning("%s fetch failed for %s: %s", name, iso, e)
            raw[name] = None

    WELLNESS_RAW_DIR.mkdir(exist_ok=True)
    (WELLNESS_RAW_DIR / f"{iso}.json").write_text(json.dumps(raw, indent=2, default=str))

    hr = raw["heart_rate"] or {}
    hrv_summary = (raw["hrv"] or {}).get("hrvSummary") or {}
    hrv_baseline = hrv_summary.get("baseline") or {}
    sleep = raw["sleep"] or {}
    sleep_scores = sleep.get("sleepScores") or {}
    sleep_overall = sleep_scores.get("overall") or {}
    # bodyBattery/reports/daily, steps, and stress all return a list (one entry
    # per requested day) rather than a single object.
    battery = (raw["body_battery"] or [{}])[0] if raw["body_battery"] else {}
    battery_values = [v[1] for v in (battery.get("bodyBatteryValuesArray") or [])]
    steps_day = (raw["steps"] or [{}])[0] if raw["steps"] else {}
    stress_day = (raw["stress"] or [{}])[0] if raw["stress"] else {}
    stress_values = stress_day.get("values") or {}

    resting_hr = first(hr, "restingHeartRate", "restingHeartRateInBeatsPerMinute")
    hr_min = first(hr, "minHeartRate")
    hr_max = first(hr, "maxHeartRate")
    hr_7day_avg = first(hr, "lastSevenDaysAvgRestingHeartRate")

    hrv_avg = first(hrv_summary, "lastNightAvg", "weeklyAvg", "lastNight5MinHigh")
    hrv_weekly_avg = first(hrv_summary, "weeklyAvg")
    hrv_5min_high = first(hrv_summary, "lastNight5MinHigh")
    hrv_status = first(hrv_summary, "status")

    sleep_score = first(sleep_overall, "value") or first(sleep, "sleepScore", "overallSleepScore")
    sleep_seconds = first(sleep, "sleepTimeSeconds", "totalSleepSeconds", "sleepDurationInSeconds")
    sleep_hours = round(sleep_seconds / 3600, 2) if sleep_seconds else None
    deep_sleep_min = round(sleep["deepSleepSeconds"] / 60, 1) if sleep.get("deepSleepSeconds") is not None else None
    light_sleep_min = round(sleep["lightSleepSeconds"] / 60, 1) if sleep.get("lightSleepSeconds") is not None else None
    rem_sleep_min = round(sleep["remSleepSeconds"] / 60, 1) if sleep.get("remSleepSeconds") is not None else None
    awake_min = round(sleep["awakeSleepSeconds"] / 60, 1) if sleep.get("awakeSleepSeconds") is not None else None
    awake_count = first(sleep, "awakeCount")
    avg_respiration = first(sleep, "averageRespirationValue")
    avg_sleep_stress = first(sleep, "avgSleepStress")
    sleep_avg_hr = first(sleep, "avgHeartRate")

    battery_charged = first(battery, "charged")
    battery_drained = first(battery, "drained")
    battery_highest = max(battery_values) if battery_values else None
    battery_lowest = min(battery_values) if battery_values else None
    battery_start = battery_values[0] if battery_values else None
    battery_end = battery_values[-1] if battery_values else None

    steps = first(steps_day, "totalSteps")
    step_goal = first(steps_day, "stepGoal")

    # All-day stress durations come back in seconds; convert to minutes like the
    # sleep-stage fields above. overallStressLevel is Garmin's 0-100 daily score.
    # Garmin returns an explicit `null` (not 0, not a missing key) for a stress
    # band with no minutes in it - e.g. highStressDuration: null on a calm day -
    # so treat null-but-present-block as 0 rather than "missing data". Only
    # treat the whole thing as missing if stress_values itself is empty (the
    # fetch failed), not when one sub-field happens to be null.
    stress_overall = first(stress_values, "overallStressLevel")
    stress_rest_min = round((stress_values.get("restStressDuration") or 0) / 60, 1) if stress_values else None
    stress_low_min = round((stress_values.get("lowStressDuration") or 0) / 60, 1) if stress_values else None
    stress_medium_min = round((stress_values.get("mediumStressDuration") or 0) / 60, 1) if stress_values else None
    stress_high_min = round((stress_values.get("highStressDuration") or 0) / 60, 1) if stress_values else None

    row = {
        "date": iso,
        "resting_hr": resting_hr, "hr_min": hr_min, "hr_max": hr_max, "hr_7day_avg": hr_7day_avg,
        "hrv_last_night_avg": hrv_avg, "hrv_weekly_avg": hrv_weekly_avg,
        "hrv_5min_high": hrv_5min_high, "hrv_status": hrv_status,
        "sleep_score": sleep_score, "sleep_hours": sleep_hours,
        "deep_sleep_min": deep_sleep_min, "light_sleep_min": light_sleep_min,
        "rem_sleep_min": rem_sleep_min, "awake_min": awake_min, "awake_count": awake_count,
        "avg_respiration": avg_respiration, "avg_sleep_stress": avg_sleep_stress,
        "sleep_avg_hr": sleep_avg_hr,
        "body_battery_charged": battery_charged, "body_battery_drained": battery_drained,
        "body_battery_highest": battery_highest, "body_battery_lowest": battery_lowest,
        "body_battery_start": battery_start, "body_battery_end": battery_end,
        "steps": steps, "step_goal": step_goal,
        "stress_overall": stress_overall, "stress_rest_min": stress_rest_min,
        "stress_low_min": stress_low_min, "stress_medium_min": stress_medium_min,
        "stress_high_min": stress_high_min,
    }

    missing = [name for name, val in row.items() if name != "date" and val is None]
    if missing:
        log.warning(
            "%s: could not extract %s from API response - inspect wellness_raw/%s.json "
            "and add the real field name(s) to the relevant extraction in fetch_training_data.py",
            iso, ", ".join(missing), iso,
        )

    return row


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
    """Upsert by date, merging field-by-field rather than replacing the whole
    row. A day that fails to fetch (rate-limited, API hiccup, etc.) returns a
    row of all-None values - without merging, that would silently overwrite
    a previously-fetched good row with blanks.
    """
    rows = {}
    if WELLNESS_CSV.exists():
        with WELLNESS_CSV.open(newline="") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = r

    existing = rows.get(row["date"], {})
    merged = dict(existing)
    for key, value in row.items():
        if value is not None and value != "":
            merged[key] = value
    rows[row["date"]] = merged

    with WELLNESS_CSV.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, restval="")
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

    for i, day in enumerate(days):
        if i > 0:
            time.sleep(0.5)  # light pacing so a long backfill doesn't trip rate limiting
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
