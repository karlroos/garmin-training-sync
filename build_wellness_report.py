#!/usr/bin/env python3
"""Builds wellness.csv into a self-contained HTML report (wellness_report.html).

Always newest-first (both the day strip and every table), with the detailed
tables split into one tab per calendar month so the page doesn't grow an
ever-longer single table as more months of data accumulate.

A short editorial callout (the "here's what's notable today" paragraph) is
intentionally NOT auto-generated here - that takes judgment a script can't
make. Pass one in with --callout/--callout-tone, or edit the placeholder
<!-- CALLOUT --> comment in the output file afterwards.
"""

import argparse
import base64
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WELLNESS_CSV = ROOT / "wellness.csv"
OUTPUT_HTML = ROOT / "wellness_report.html"

# ============================================================================
# PERSONAL THRESHOLDS - CHANGE THESE TO FIT YOU
#
# These are tuned to one athlete's own normal resting HR (~38-44 bpm) and
# overnight HRV (~54-81 ms) baseline. If you run this on your own data, find
# your own normal ranges first (a few weeks of wellness.csv will show them)
# and replace the numbers below - as given, they will under- or over-flag on
# anyone whose resting HR/HRV baseline differs from the original athlete's.
# ============================================================================
ALERT_SLEEP_SCORE_BELOW = 40     # sleep score under this -> ALERT (very poor night)
ALERT_RESTING_HR_AT_OR_ABOVE = 46  # resting HR at/above this -> ALERT (genuinely elevated)
WATCH_RESTING_HR_AT_OR_ABOVE = 44  # resting HR at/above this -> WATCH (mildly elevated)
WATCH_HRV_BELOW = 54               # overnight HRV under this -> WATCH (below personal baseline)
WATCH_SLEEP_SCORE_BELOW = 70       # sleep score under this -> flag the stat card (not full WATCH)
# ============================================================================

TONE_STYLES = {
    "ok": ("var(--ok-bg)", "#CFE4D7", "var(--ok)"),
    "watch": ("var(--watch-bg)", "#E9CFA3", "var(--watch)"),
    "alert": ("var(--alert-bg)", "#E3A8A8", "var(--alert)"),
}


def num(v, default="0"):
    if v in (None, ""):
        return default
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def severity(r):
    """ALERT is reserved for a genuinely acute day (very poor sleep or a high
    resting HR). A lingering UNBALANCED/LOW HRV status on its own, once the
    core HR/HRV numbers are already recovering, stays at WATCH rather than
    escalating - see 15-16 Nov 2026 for why this distinction matters.
    """
    rhr = float(r["resting_hr"])
    hrv = float(r["hrv_last_night_avg"])
    sleep_score = float(r["sleep_score"])
    status = r["hrv_status"]
    if sleep_score < ALERT_SLEEP_SCORE_BELOW or rhr >= ALERT_RESTING_HR_AT_OR_ABOVE:
        return "alert"
    if status in ("UNBALANCED", "LOW") or hrv < WATCH_HRV_BELOW or rhr >= WATCH_RESTING_HR_AT_OR_ABOVE:
        return "watch"
    return "ok"


def row_class(r):
    sev = severity(r)
    if sev == "alert":
        return ' class="watch" style="background:#F3D9D9;"'
    if sev == "watch":
        return ' class="watch"'
    return ""


def date_label(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%a %-d %b")


def date_short(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %b")


def month_key_label(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%Y-%m"), d.strftime("%B %Y")


def build_daystrip(rows_desc):
    chips = []
    for r in rows_desc:
        sev = severity(r)
        css = " watch" if sev in ("watch", "alert") else ""
        style = ' style="background:#F3D9D9;"' if sev == "alert" else ""
        label = {"ok": "OK", "watch": "WATCH", "alert": "ALERT"}[sev]
        label_style = ' style="color:#B33A3A;"' if sev == "alert" else ""
        chips.append(
            f'<div class="daychip{css}"{style}><div class="d">{date_short(r["date"])}</div>'
            f'<div class="n"{label_style}>{label}</div></div>'
        )
    return "\n    ".join(chips)


def build_stats(latest):
    # Only flag individual cards when that specific metric is the reason for concern,
    # not just because some other field on the same day is bad.
    rhr_flag = " flag" if float(latest["resting_hr"]) >= WATCH_RESTING_HR_AT_OR_ABOVE else ""
    hrv_flag = " flag" if latest["hrv_status"] in ("UNBALANCED", "LOW") or float(latest["hrv_last_night_avg"]) < WATCH_HRV_BELOW else ""
    sleep_flag = " flag" if float(latest["sleep_score"]) < WATCH_SLEEP_SCORE_BELOW else ""
    date_lbl = date_short(latest["date"])
    hrv_note = ""
    if latest["hrv_status"] in ("UNBALANCED", "LOW"):
        hrv_note = f'<div style="font-size:11px;color:var(--watch);font-weight:600;margin-top:2px;">Garmin: {latest["hrv_status"]}</div>'
    return f"""
  <div class="stats">
    <div class="stat{rhr_flag}">
      <div class="label">Resting HR, {date_lbl}</div>
      <div class="value">{num(latest["resting_hr"])}<span class="unit"> bpm</span></div>
    </div>
    <div class="stat{hrv_flag}">
      <div class="label">HRV, {date_lbl}</div>
      <div class="value">{num(latest["hrv_last_night_avg"])}<span class="unit"> ms</span></div>
      {hrv_note}
    </div>
    <div class="stat{sleep_flag}">
      <div class="label">Sleep score, {date_lbl}</div>
      <div class="value">{num(latest["sleep_score"])}</div>
    </div>
    <div class="stat">
      <div class="label">Body battery end, {date_lbl}</div>
      <div class="value">{num(latest["body_battery_end"])}</div>
    </div>
  </div>"""


def build_month_panel(month_key, month_label, rows, active):
    def rows_html(cells_fn):
        return "\n".join(f"<tr{row_class(r)}>{cells_fn(r)}</tr>" for r in rows)

    hr_rows = rows_html(lambda r: (
        f'<td class="date">{date_label(r["date"])}</td><td>{num(r["resting_hr"])}</td>'
        f'<td>{num(r["hr_min"])}</td><td>{num(r["hr_max"])}</td><td>{num(r["hr_7day_avg"])}</td>'
    ))
    hrv_rows = rows_html(lambda r: (
        f'<td class="date">{date_label(r["date"])}</td><td>{num(r["hrv_last_night_avg"])}</td>'
        f'<td>{num(r["hrv_weekly_avg"])}</td><td>{num(r["hrv_5min_high"])}</td><td>{r["hrv_status"]}</td>'
    ))
    sleep_rows = rows_html(lambda r: (
        f'<td class="date">{date_label(r["date"])}</td><td>{num(r["sleep_score"])}</td><td>{r["sleep_hours"]}</td>'
        f'<td>{num(r["deep_sleep_min"])}m</td><td>{num(r["light_sleep_min"])}m</td><td>{num(r["rem_sleep_min"])}m</td>'
        f'<td>{num(r["awake_min"])}m</td><td>{num(r["awake_count"])}</td><td>{num(r["avg_respiration"])}</td>'
        f'<td>{num(r["avg_sleep_stress"])}</td><td>{num(r["sleep_avg_hr"])}</td>'
    ))
    battery_rows = rows_html(lambda r: (
        f'<td class="date">{date_label(r["date"])}</td><td>{r["body_battery_charged"]}</td>'
        f'<td>{r["body_battery_drained"]}</td><td>{r["body_battery_highest"]}</td>'
        f'<td>{r["body_battery_lowest"]}</td><td>{r["body_battery_start"]}</td><td>{r["body_battery_end"]}</td>'
    ))

    def steps_cells(r):
        steps, goal = r["steps"], r["step_goal"]
        pct = round(float(steps) / float(goal) * 100) if steps and goal else 0
        return f'<td class="date">{date_label(r["date"])}</td><td>{num(steps)}</td><td>{num(goal)}</td><td>{pct}%</td>'
    steps_rows = rows_html(steps_cells)

    stress_rows = rows_html(lambda r: (
        f'<td class="date">{date_label(r["date"])}</td><td>{num(r["stress_overall"])}</td>'
        f'<td>{num(r["stress_rest_min"])}</td><td>{num(r["stress_low_min"])}</td>'
        f'<td>{num(r["stress_medium_min"])}</td><td>{num(r["stress_high_min"])}</td>'
    ))

    hidden = "" if active else " hidden"
    return f"""
  <div class="tabpanel" data-month="{month_key}"{hidden}>
    <section>
      <h2>Heart rate &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Resting</th><th>Min</th><th>Max</th><th>7-day avg</th></tr></thead>
        <tbody>
{hr_rows}
        </tbody>
      </table></div>
    </section>
    <section>
      <h2>HRV (ms) &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Last night</th><th>Weekly avg</th><th>5-min high</th><th>Status</th></tr></thead>
        <tbody>
{hrv_rows}
        </tbody>
      </table></div>
    </section>
    <section>
      <h2>Sleep &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Score</th><th>Hours</th><th>Deep</th><th>Light</th><th>REM</th><th>Awake</th><th>Wakes</th><th>Resp.</th><th>Stress</th><th>Avg HR</th></tr></thead>
        <tbody>
{sleep_rows}
        </tbody>
      </table></div>
    </section>
    <section>
      <h2>Body battery &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Charged</th><th>Drained</th><th>Highest</th><th>Lowest</th><th>Start</th><th>End</th></tr></thead>
        <tbody>
{battery_rows}
        </tbody>
      </table></div>
    </section>
    <section>
      <h2>Steps &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Steps</th><th>Goal</th><th>% of goal</th></tr></thead>
        <tbody>
{steps_rows}
        </tbody>
      </table></div>
    </section>
    <section>
      <h2>All-day stress &mdash; {month_label}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Date</th><th>Overall</th><th>Rest (min)</th><th>Low (min)</th><th>Medium (min)</th><th>High (min)</th></tr></thead>
        <tbody>
{stress_rows}
        </tbody>
      </table></div>
    </section>
  </div>"""


def build(callout_html=None, callout_tone="watch"):
    rows = list(csv.DictReader(WELLNESS_CSV.open()))
    rows_desc = list(reversed(rows))
    latest = rows_desc[0]
    first_date, last_date = rows[0]["date"], rows[-1]["date"]

    months = defaultdict(list)
    month_labels = {}
    for r in rows_desc:
        key, label = month_key_label(r["date"])
        months[key].append(r)
        month_labels[key] = label
    month_keys_desc = sorted(months.keys(), reverse=True)

    tab_buttons = "\n    ".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-month="{k}">{month_labels[k]} ({len(months[k])})</button>'
        for i, k in enumerate(month_keys_desc)
    )
    tab_panels = "\n".join(
        build_month_panel(k, month_labels[k], months[k], active=(i == 0))
        for i, k in enumerate(month_keys_desc)
    )

    callout_block = ""
    if callout_html:
        bg, border, fg = TONE_STYLES.get(callout_tone, TONE_STYLES["watch"])
        callout_block = f"""
  <div class="callout" style="background:{bg};border-color:{border};">
    <div class="dot" style="background:{fg};"></div>
    <div>{callout_html}</div>
  </div>"""
    else:
        callout_block = "\n  <!-- CALLOUT -->"

    csv_b64 = base64.b64encode(WELLNESS_CSV.read_bytes()).decode()
    first_short = datetime.strptime(first_date, "%Y-%m-%d").strftime("%-d %b")
    last_short = datetime.strptime(last_date, "%Y-%m-%d").strftime("%-d %b %Y")

    html = f"""<title>Wellness export &mdash; {first_short}&ndash;{last_short}</title>
<style>
  :root{{
    --bg:#F6F7F5; --card:#FFFFFF; --ink:#1B2420; --muted:#6E7873; --line:#E1E6E2;
    --accent:#2E6F6A; --accent-ink:#F3FAF9;
    --ok:#3F7D5C; --ok-bg:#EBF4EE;
    --watch:#AD6A1F; --watch-bg:#FBF0E2;
    --alert:#B33A3A; --alert-bg:#F3D9D9;
  }}
  *{{box-sizing:border-box;}}
  body{{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    display:flex; justify-content:center; padding:48px 20px;
  }}
  .sheet{{
    width:100%; max-width:960px; background:var(--card); border:1px solid var(--line);
    border-radius:14px; padding:36px 40px 28px;
  }}
  header{{
    display:flex; justify-content:space-between; align-items:flex-start; gap:24px;
    flex-wrap:wrap; border-bottom:1px solid var(--line); padding-bottom:22px; margin-bottom:22px;
  }}
  h1{{
    font-family:Georgia,"Iowan Old Style","Palatino Linotype",serif; font-size:28px;
    font-weight:600; margin:0 0 6px; letter-spacing:-0.01em; text-wrap:balance;
  }}
  .subtitle{{ margin:0; color:var(--muted); font-size:14px; line-height:1.5; }}
  .subtitle code{{
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px;
    background:var(--bg); padding:1px 5px; border-radius:4px; border:1px solid var(--line);
  }}
  .dl{{
    display:inline-flex; align-items:center; gap:8px; background:var(--accent); color:var(--accent-ink);
    text-decoration:none; font-size:14px; font-weight:600; padding:11px 18px; border-radius:8px;
    white-space:nowrap; transition:filter 0.15s ease;
  }}
  .dl:hover{{ filter:brightness(1.08); }}
  .dl:focus-visible{{ outline:2px solid var(--accent); outline-offset:3px; }}
  .dl svg{{ width:15px; height:15px; flex:none; }}
  .callout{{
    display:flex; gap:12px; align-items:flex-start; border:1px solid; border-radius:10px;
    padding:16px 18px; margin-bottom:22px; font-size:13.5px; line-height:1.6;
  }}
  .callout .dot{{ flex:none; width:9px; height:9px; border-radius:50%; margin-top:6px; }}
  .callout b{{ color:inherit; }}
  .stats{{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }}
  .stat{{ background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .stat.flag{{ background:var(--watch-bg); border-color:#E9CFA3; }}
  .stat .label{{ font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); margin-bottom:6px; }}
  .stat .value{{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-variant-numeric:tabular-nums; font-size:22px; font-weight:600; }}
  .stat.flag .value{{ color:var(--watch); }}
  .stat .unit{{ font-size:13px; font-weight:400; color:var(--muted); }}
  .daystrip{{ display:flex; gap:6px; overflow-x:auto; padding-bottom:4px; margin-bottom:26px; }}
  .daychip{{ flex:none; text-align:center; min-width:52px; border-radius:8px; padding:7px 4px 6px; background:var(--ok-bg); }}
  .daychip.watch{{ background:var(--watch-bg); }}
  .daychip .d{{ font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:0.03em; }}
  .daychip .n{{ font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; color:var(--ok); }}
  .daychip.watch .n{{ color:var(--watch); }}
  .tabs{{ display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:22px; overflow-x:auto; }}
  .tab{{
    appearance:none; border:none; background:none; cursor:pointer; font:inherit; font-size:13px;
    font-weight:600; color:var(--muted); padding:9px 14px; border-bottom:2px solid transparent;
    white-space:nowrap;
  }}
  .tab:hover{{ color:var(--ink); }}
  .tab.active{{ color:var(--accent); border-bottom-color:var(--accent); }}
  .tab:focus-visible{{ outline:2px solid var(--accent); outline-offset:2px; }}
  section{{ margin-bottom:28px; }}
  section:last-of-type{{ margin-bottom:0; }}
  h2{{ font-size:12px; text-transform:uppercase; letter-spacing:0.07em; color:var(--muted); font-weight:700; margin:0 0 10px; }}
  .table-wrap{{ overflow-x:auto; max-height:320px; overflow-y:auto; }}
  table{{ width:100%; min-width:520px; border-collapse:collapse; font-size:13.5px; }}
  thead th{{
    position:sticky; top:0; background:var(--card); text-align:right; font-size:11px; text-transform:uppercase;
    letter-spacing:0.05em; color:var(--muted); font-weight:600; padding:0 10px 8px; border-bottom:1px solid var(--line);
    white-space:nowrap;
  }}
  thead th:first-child, tbody td.date{{ text-align:left; }}
  tbody td{{
    padding:8px 10px; border-bottom:1px solid var(--line); font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-variant-numeric:tabular-nums; white-space:nowrap; text-align:right;
  }}
  tbody td.date{{ font-family:inherit; font-variant-numeric:normal; }}
  tbody tr:last-child td{{ border-bottom:none; }}
  tbody tr.watch td{{ background:var(--watch-bg); }}
  footer{{ margin-top:8px; padding-top:16px; border-top:1px solid var(--line); font-size:12px; color:var(--muted); line-height:1.6; }}
  footer code{{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
</style>

<div class="sheet">
  <header>
    <div>
      <h1>Wellness, {first_short}&ndash;{last_short}</h1>
      <p class="subtitle">Heart rate &middot; HRV &middot; sleep &middot; body battery &middot; steps &middot; all-day stress &mdash; synced from Garmin via <code>fetch_training_data.py</code>. Newest first, one tab per month.</p>
    </div>
    <a class="dl" download="wellness.csv" href="data:text/csv;base64,{csv_b64}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16"/></svg>
      wellness.csv
    </a>
  </header>
{callout_block}
{build_stats(latest)}

  <div class="daystrip">
    {build_daystrip(rows_desc)}
  </div>

  <div class="tabs">
    {tab_buttons}
  </div>
{tab_panels}

  <footer>
    Newest-first throughout, one tab per calendar month &mdash; click a month to see its daily tables.
    Steps and all-day stress: "% of goal" is against a 10,000-step goal; stress is Garmin's 0&ndash;100 daily score plus minutes spent in each stress band.
    Day-strip/row status: WATCH = overnight HRV under {num(WATCH_HRV_BELOW)}&nbsp;ms, resting HR &ge;{num(WATCH_RESTING_HR_AT_OR_ABOVE)}&nbsp;bpm, or Garmin's own HRV status reading UNBALANCED/LOW. ALERT = sleep score under {num(ALERT_SLEEP_SCORE_BELOW)} or resting HR &ge;{num(ALERT_RESTING_HR_AT_OR_ABOVE)} &mdash; reserved for a genuinely acute day; a lingering UNBALANCED/LOW status alone stays at WATCH once the core numbers are recovering. These thresholds are personal (see the top of <code>build_wellness_report.py</code>) &mdash; not universal normal ranges.
    This table is daily summaries only; full per-minute HR, 5-minute HRV, and body-battery curves live in <code>wellness_raw/&lt;date&gt;.json</code> on request.
    Source file: <code>wellness.csv</code>.
  </footer>
</div>

<script>
  document.querySelectorAll('.tab').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.tab').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      var month = btn.dataset.month;
      document.querySelectorAll('.tabpanel').forEach(function(panel) {{
        panel.hidden = panel.dataset.month !== month;
      }});
    }});
  }});
</script>
"""
    OUTPUT_HTML.write_text(html)
    return OUTPUT_HTML


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callout", help="HTML for the editorial callout banner (omit for none)")
    parser.add_argument("--callout-tone", choices=["ok", "watch", "alert"], default="watch")
    args = parser.parse_args()
    path = build(callout_html=args.callout, callout_tone=args.callout_tone)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
