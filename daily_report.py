"""
Daily Report Emailer
--------------------
Runs as a daemon thread on the "reporter" bot (Paul's deployment). Once a day
at REPORT_HOUR_ET (default 17:00 America/New_York, i.e. 5pm ET, DST-aware) it:

  1. Pulls /report JSON from every account in REPORT_SOURCES
  2. Builds a consolidated HTML performance report for all accounts
  3. Emails it via the Resend API to REPORT_TO

Env vars (set on the reporter bot's Railway):
  RESEND_API_KEY   -  your Resend API key (required to send)
  REPORT_TO        -  recipient email (default paulmoreland35@gmail.com)
  REPORT_FROM      -  sender (default "Olympus Bot <onboarding@resend.dev>")
  REPORT_SOURCES   -  comma list of "Label=url" report endpoints. Default:
                      both known bot /report URLs.
  REPORT_HOUR_ET   -  hour of day (ET, 24h) to send. Default 17.
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tzdata missing
    _ET = None

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = (
    "Paul=https://spectacular-contentment-production-0410.up.railway.app/report,"
    "Derrick=https://web-production-b75628.up.railway.app/report"
)


def _parse_sources() -> list[tuple[str, str]]:
    raw = os.getenv("REPORT_SOURCES", _DEFAULT_SOURCES)
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        label, url = chunk.split("=", 1)
        out.append((label.strip(), url.strip()))
    return out


def _fetch(url: str) -> dict:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[Report] Could not fetch {url}: {e}")
        return {"fetch_error": str(e)}


# ---------------------------------------------------------------------------
# Insight generation — data-driven "where can we grow / is the scanner ok"
# ---------------------------------------------------------------------------

def _insights(label: str, d: dict) -> list[str]:
    notes = []
    if d.get("fetch_error") or d.get("broker_error"):
        notes.append(f"&#9888;&#65039; {label}: bot unreachable or broker error "
                     f"({d.get('fetch_error') or d.get('broker_error')}).")
        return notes

    bal = d.get("balance", 0)
    if bal < 10:
        notes.append(f"&#128308; {label}: balance critically low (${bal:,.2f}) — "
                     f"deposit funds before trading resumes.")
    elif bal < 50:
        notes.append(f"&#128993; {label}: low balance (${bal:,.2f}) — position sizes will be tiny.")

    sc = d.get("scanner", {})
    if not sc.get("api_key_set"):
        notes.append(f"&#9888;&#65039; {label}: scanner OFF — no TWELVE_DATA_API_KEY set. "
                     f"It is not scanning for setups.")
    elif not sc.get("running"):
        notes.append(f"&#9888;&#65039; {label}: scanner thread not running — check the deploy.")
    else:
        last = sc.get("last_scan_utc")
        if last:
            try:
                age_h = (datetime.utcnow() - datetime.fromisoformat(last.replace("Z", "")).replace(tzinfo=None)).total_seconds() / 3600
                if age_h > 5:
                    notes.append(f"&#9888;&#65039; {label}: scanner last ran {age_h:.1f}h ago — "
                                 f"should scan every 4h. May be stuck.")
                else:
                    notes.append(f"&#9989; {label}: scanner healthy (last scan {age_h:.1f}h ago, "
                                 f"{len(sc.get('symbols', []))} symbols).")
            except Exception:
                notes.append(f"&#9989; {label}: scanner running ({len(sc.get('symbols', []))} symbols).")
        else:
            notes.append(f"&#9989; {label}: scanner running but no scan completed yet "
                         f"({len(sc.get('symbols', []))} symbols).")

    stats = d.get("stats", {})
    if stats.get("total_closed", 0) > 0:
        wr = stats.get("win_rate_pct", 0)
        if wr < 40:
            notes.append(f"&#128201; {label}: win rate {wr}% over {stats['total_closed']} trades — "
                         f"below target. Review entry filters / confluence threshold.")
        elif wr >= 55:
            notes.append(f"&#128200; {label}: strong win rate {wr}% over {stats['total_closed']} trades. "
                         f"Consider scaling risk slightly.")

    dd = d.get("drawdown_halted")
    if dd:
        notes.append(f"&#9940; {label}: trading HALTED today — daily drawdown limit hit.")

    return notes


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)


def _account_section(label: str, d: dict) -> str:
    if d.get("fetch_error"):
        return f"<h2>{label}</h2><p style='color:#c00'>Could not reach bot: {d['fetch_error']}</p>"

    bal      = _fmt_money(d.get("balance", 0))
    acct     = d.get("account_id", "?")
    server   = d.get("server", "?")
    day_pnl  = d.get("day_pnl")
    stats    = d.get("stats", {})
    opens    = d.get("open_positions", [])
    closes   = d.get("closed_trades", [])

    day_line = ""
    if day_pnl is not None:
        color = "#0a0" if day_pnl >= 0 else "#c00"
        day_line = (f"<tr><td>Today's P&amp;L</td>"
                    f"<td style='color:{color}'><b>{_fmt_money(day_pnl)} "
                    f"({d.get('day_pnl_pct', 0):+.2f}%)</b></td></tr>")

    rows = (
        f"<tr><td>Account</td><td>#{acct} ({server})</td></tr>"
        f"<tr><td>Balance</td><td><b>{bal}</b></td></tr>"
        f"{day_line}"
        f"<tr><td>Closed today</td><td>{stats.get('total_closed', 0)} "
        f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L, "
        f"{stats.get('win_rate_pct', 0)}% win)</td></tr>"
        f"<tr><td>Open positions</td><td>{len(opens)}</td></tr>"
    )

    html = (f"<h2 style='margin-bottom:4px'>{label}</h2>"
            f"<table cellpadding='6' style='border-collapse:collapse;font-size:14px'>"
            f"{rows}</table>")

    if closes:
        html += "<h4 style='margin:12px 0 4px'>Closed today</h4><table cellpadding='5' " \
                "style='border-collapse:collapse;font-size:13px;border:1px solid #ddd'>" \
                "<tr style='background:#f4f4f4'><th>Ticker</th><th>Side</th><th>Entry</th>" \
                "<th>Exit</th><th>Move</th><th>Result</th><th>Hit</th></tr>"
        for c in closes:
            won = str(c.get("outcome", "")).upper() == "WIN"
            color = "#0a0" if won else "#c00"
            html += (f"<tr><td>{c.get('ticker')}</td><td>{c.get('side')}</td>"
                     f"<td>{c.get('entry')}</td><td>{c.get('exit')}</td>"
                     f"<td>{c.get('move')}</td>"
                     f"<td style='color:{color}'><b>{c.get('outcome')}</b></td>"
                     f"<td>{c.get('reason', '')}</td></tr>")
        html += "</table>"

    if opens:
        html += "<h4 style='margin:12px 0 4px'>Open positions</h4><table cellpadding='5' " \
                "style='border-collapse:collapse;font-size:13px;border:1px solid #ddd'>" \
                "<tr style='background:#f4f4f4'><th>Ticker</th><th>Side</th><th>Qty</th>" \
                "<th>Entry</th><th>SL</th><th>TP</th><th>Unreal. P&amp;L</th></tr>"
        for p in opens:
            html += (f"<tr><td>{p.get('ticker')}</td><td>{p.get('side')}</td>"
                     f"<td>{p.get('qty')}</td><td>{p.get('entry')}</td>"
                     f"<td>{p.get('sl')}</td><td>{p.get('tp')}</td>"
                     f"<td>{_fmt_money(p.get('unrealised_pnl', 0))}</td></tr>")
        html += "</table>"

    return html


def build_report_html() -> tuple[str, str]:
    """Returns (subject, html_body)."""
    sources = _parse_sources()
    sections, all_notes = [], []
    total_bal = 0.0

    for label, url in sources:
        d = _fetch(url)
        sections.append(_account_section(label, d))
        all_notes.extend(_insights(label, d))
        try:
            total_bal += float(d.get("balance", 0))
        except Exception:
            pass

    today = datetime.now(_ET).strftime("%A, %B %d %Y") if _ET else datetime.utcnow().strftime("%Y-%m-%d")

    notes_html = ""
    if all_notes:
        notes_html = ("<h2>Insights &amp; growth</h2><ul style='font-size:14px;line-height:1.6'>"
                      + "".join(f"<li>{n}</li>" for n in all_notes) + "</ul>")

    body = (
        f"<div style='font-family:Arial,Helvetica,sans-serif;max-width:680px;margin:auto'>"
        f"<h1 style='color:#1a1a2e'>Olympus Bot &mdash; Daily Report</h1>"
        f"<p style='color:#666'>{today} &bull; combined balance "
        f"<b>{_fmt_money(total_bal)}</b></p>"
        f"<hr>"
        + "<hr>".join(sections)
        + "<hr>"
        + notes_html
        + "<p style='color:#999;font-size:12px;margin-top:24px'>"
          "Automated report from your Olympus trading bot. P&amp;L and positions "
          "are pulled live from TradeLocker.</p></div>"
    )
    subject = f"Olympus Daily Report — {today} — Combined {_fmt_money(total_bal)}"
    return subject, body


def send_email(subject: str, html: str) -> bool:
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        logger.error("[Report] RESEND_API_KEY not set — cannot send email.")
        return False
    to_addr   = os.getenv("REPORT_TO", "paulmoreland35@gmail.com")
    from_addr = os.getenv("REPORT_FROM", "Olympus Bot <onboarding@resend.dev>")
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"from": from_addr, "to": [to_addr],
                  "subject": subject, "html": html},
            timeout=30,
        )
        if r.status_code >= 400:
            logger.error(f"[Report] Resend error {r.status_code}: {r.text}")
            return False
        logger.info(f"[Report] Email sent to {to_addr}.")
        return True
    except Exception as e:
        logger.error(f"[Report] Email send failed: {e}")
        return False


def send_report_now() -> bool:
    """Build and send immediately — used for manual triggers/testing."""
    subject, html = build_report_html()
    return send_email(subject, html)


def _seconds_until_next_run(hour: int) -> float:
    if _ET is None:
        # Fallback: treat hour as UTC
        now = datetime.utcnow()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
    now = datetime.now(_ET)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _report_loop():
    hour = int(os.getenv("REPORT_HOUR_ET", "17"))
    logger.info(f"[Report] Daily report loop started — sends at {hour}:00 ET.")
    while True:
        wait = _seconds_until_next_run(hour)
        logger.info(f"[Report] Next report in {wait/3600:.1f}h.")
        time.sleep(wait)
        try:
            send_report_now()
        except Exception as e:
            logger.error(f"[Report] Report cycle failed: {e}")
        time.sleep(60)  # avoid double-fire within the same minute


def start_report_thread():
    t = threading.Thread(target=_report_loop, daemon=True, name="daily-report")
    t.start()
    logger.info("[Report] Daily report thread started.")
    return t
