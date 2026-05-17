#!/usr/bin/env python3
"""
Vatican Museums Admission Ticket Monitor

Monitors the official Vatican Museums ticketing API for:
  "Vatican Museums - Admission Ticket" availability on specific dates.

Usage:
    python3 vatican_monitor.py                     # Default: 2 visitors, 5 min
    python3 vatican_monitor.py --visitors 4        # 4 visitors
    python3 vatican_monitor.py --once              # Single check only
    python3 vatican_monitor.py --interval 120      # Poll every 2 minutes
    python3 vatican_monitor.py --notify            # Desktop notification on find
    python3 vatican_monitor.py --log               # Write log to file
    python3 vatican_monitor.py --telegram <TOKEN> <CHAT_ID>  # Telegram alerts

For GitHub Actions, set env vars TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
instead of passing --telegram on the command line.

Requirements: pip3 install requests
"""

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Optional

try:
    import requests
except ImportError:
    print("❌ pip3 install requests")
    sys.exit(1)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

BASE_URL = "https://tickets.museivaticani.va"
TAG = "MV-Biglietti"
AREA_ID = "1"

# Admission Ticket name pattern (the Vatican Museums main entry ticket)
ADMISSION_TICKET_NAME = "Vatican Museums - Admission Ticket"
# Alternative names for the same ticket (e.g., Sundays or special events)
ADMISSION_TICKET_ALT_NAMES = [
    "sunday in the museums",
    "vatican museums - admission",
]
# Additional tickets to monitor besides the admission ticket
EXTRA_TICKETS = [
    "Vatican Museums - Vatican City Tours - Open Bus Gardens",
]

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 "
        "Safari/605.1.15"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": f"{BASE_URL}/home/fromtag/4/1780124400000/MV-Biglietti/1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─── LOGGING ────────────────────────────────────────────────────────────────

class Logger:
    """Print to both stdout and optional log file."""

    def __init__(self, log_path: Optional[str] = None):
        self.log_file = open(log_path, "a", encoding="utf-8") if log_path else None

    def write(self, msg: str = ""):
        print(msg)
        if self.log_file:
            self.log_file.write(msg + "\n")
            self.log_file.flush()

    def close(self):
        if self.log_file:
            self.log_file.close()


def setup_log(log_path: Optional[str]):
    """Redirect print() to also write to log file."""
    if not log_path:
        return
    builtin_print = __builtins__.print
    log_file = open(log_path, "a", encoding="utf-8")

    def tee_print(*args, **kwargs):
        builtin_print(*args, **kwargs)
        msg = " ".join(str(a) for a in args)
        log_file.write(msg + "\n")
        log_file.flush()

    __builtins__.print = tee_print


# ─── API ─────────────────────────────────────────────────────────────────────

def check_availability(visitor_num: int, date_str: str) -> Optional[dict]:
    """Query search/resultPerTag for a given date and visitor count."""
    url = f"{BASE_URL}/api/search/resultPerTag"
    params = {
        "lang": "en",
        "tag": TAG,
        "areaId": AREA_ID,
        "visitorNum": visitor_num,
        "visitDate": date_str,
    }
    try:
        resp = SESSION.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  API error ({date_str}, {visitor_num}p): {e}")
        return None


def check_time_slots(visitor_num: int, date_str: str,
                     visit_type_id: int) -> Optional[dict]:
    """Query visit/timeavail for detailed time slot availability."""
    url = f"{BASE_URL}/api/visit/timeavail"
    params = {
        "lang": "en",
        "visitLang": "",
        "visitTypeId": visit_type_id,
        "visitorNum": visitor_num,
        "visitDate": date_str,
    }
    try:
        resp = SESSION.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException:
        return None


# ─── DISPLAY ─────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """macOS desktop notification."""
    os.system(
        f"osascript -e 'display notification \"{message}\" "
        f"with title \"{title}\"' 2>/dev/null"
    )


def notify_ntfy(topic: str, title: str, message: str, priority: str = "default"):
    """Send push notification to phone via ntfy.sh (free, no signup).

    Install the ntfy app on your phone and subscribe to the same topic.
    https://ntfy.sh
    """
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "ticket",
            },
            timeout=10,
        )
    except Exception:
        pass  # Don't let notification failure crash the monitor


def notify_telegram(bot_token: str, chat_id: str, message: str):
    """Send message via Telegram Bot (free).

    1. Create a bot with @BotFather on Telegram, get the token
    2. Start a chat with your bot, then get chat_id from:
       https://api.telegram.org/bot<TOKEN>/getUpdates
    """
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


ICON = {"AVAILABLE": "🟢", "LOW_AVAILABILITY": "🟡", "SOLD_OUT": "🔴"}


def _date_to_ms(date_str: str) -> int:
    """Convert dd/MM/yyyy to Unix ms at 09:00 Europe/Rome (Vatican opening time).

    Matches the site's existing URL convention so the SPA resolves the date
    correctly regardless of the user's local timezone.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Rome")
    except Exception:
        from datetime import timezone, timedelta
        tz = timezone(timedelta(hours=2))  # CEST fallback (May-Oct)
    dt = datetime.strptime(date_str, "%d/%m/%Y").replace(hour=9, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _build_deep_link(visitor_num: int, date_str: str,
                     visit_type_id: Optional[int] = None,
                     area_id: str = AREA_ID) -> str:
    """Build a clickable URL pointing directly at the ticket/date/visitors."""
    date_ms = _date_to_ms(date_str)
    if visit_type_id:
        return f"{BASE_URL}/home/visit/{visitor_num}/{date_ms}/{area_id}/{visit_type_id}"
    return f"{BASE_URL}/home/fromtag/{visitor_num}/{date_ms}/{TAG}/{area_id}"


def print_time_slots(timetable: list, before: str = "23:59"):
    """Print time slots — only available ones before cutoff, skip SOLD_OUT."""
    available = [
        s for s in timetable
        if s.get("availability") != "SOLD_OUT" and s.get("time", "24:00") <= before
    ]
    if not available:
        return
    for slot in available:
        avail = slot.get("availability", "")
        icon = ICON.get(avail, "⚪")
        residual = slot.get("residual", "?")
        print(f"         {icon} {slot['time']}  ({avail}, {residual} left)")


# ─── MAIN LOGIC ──────────────────────────────────────────────────────────────

def _find_ticket(visits: list, names: list[str], alt_names: list[str]):
    """Find a visit matching any of the given names or alt name substrings."""
    for visit in visits:
        name = visit.get("name", "")
        name_lower = name.lower()
        if name in names:
            return visit
        if any(alt in name_lower for alt in alt_names):
            return visit
    return None


def _process_ticket(visit: dict, vn: int, date_str: str, label: str,
                    before: str, findings: list = None) -> tuple[bool, bool]:
    """
    Process a single ticket visit.
    Returns (is_available, should_continue_checking).
    If `findings` list is passed, appends a dict describing what was found.
    """
    avail = visit.get("availability", "UNKNOWN")
    vid = visit.get("id")
    name = visit.get("name")
    participants = visit.get("numberParticipants", "")
    desc = visit.get("descrAvailability", "")
    price = visit.get("priceFrom", "")
    price_str = f" | €{price}" if price else ""

    if avail in ("AVAILABLE", "LOW_AVAILABILITY"):
        icon = ICON.get(avail, "🟢")
        desc_lower = (desc or "").lower()
        effectively_available = True

        actual_slots = []
        slots = None
        if vid:
            slots = check_time_slots(vn, date_str, vid)
            if slots and slots.get("timetable"):
                actual_slots = [
                    s for s in slots["timetable"]
                    if s.get("availability") != "SOLD_OUT"
                    and s.get("time", "24:00") <= before
                ]

        if "pax not available" in desc_lower:
            effectively_available = False
        if vid and slots and slots.get("timetable") and len(actual_slots) == 0:
            effectively_available = False

        if not effectively_available:
            reason = desc if desc and desc != name else "no bookable slots"
            print(f"  🟡 {label} ({vn}p) — {name}: Listed as {avail}, but {reason}")
            return False, False
        else:
            print(f"\n  🎟️  {icon} {name} — {avail}!")
            print(f"      Date: {label}")
            print(f"      Visitors: {vn} | {participants}{price_str}")
            if desc and desc != name:
                print(f"      {desc}")

            if vid and slots and slots.get("timetable"):
                avail_slots = [
                    s for s in slots["timetable"]
                    if s.get("availability") != "SOLD_OUT"
                    and s.get("time", "24:00") <= before
                ]
                if avail_slots:
                    print(f"      Time slots:")
                    print_time_slots(slots["timetable"], before)

            if findings is not None:
                avail_slots_info = []
                if vid and slots and slots.get("timetable"):
                    for s in slots["timetable"]:
                        if (s.get("availability") != "SOLD_OUT"
                                and s.get("time", "24:00") <= before):
                            avail_slots_info.append({
                                "time": s.get("time"),
                                "availability": s.get("availability"),
                                "residual": s.get("residual"),
                            })
                findings.append({
                    "ticket": name,
                    "date": date_str,
                    "label": label,
                    "visitors": vn,
                    "availability": avail,
                    "price": price,
                    "slots": avail_slots_info,
                    "visit_type_id": vid,
                    "deep_link": _build_deep_link(vn, date_str, vid),
                })
            return True, False
    else:
        print(f"  🔴 {label} ({vn}p) — {name}: {avail}")
        return False, False


# ─── MAIN LOGIC ──────────────────────────────────────────────────────────────

def check_date(date_str: str, visitor_nums: list[int], before: str = "23:59",
               log: "Logger" = None,
               findings: list = None) -> tuple[bool, bool]:
    """
    Check a single date across multiple visitor counts.
    Returns (found_available, has_any_data).
    If `findings` list is passed, available tickets are appended to it.
    """
    if log is None:
        log = Logger()
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    label = dt.strftime("%A, %B %d, %Y")

    found_available = False
    has_data = False

    for vn in visitor_nums:
        data = check_availability(vn, date_str)
        if data is None:
            continue

        has_data = True
        visits = data.get("visits", [])

        # Check primary Admission Ticket
        admission = _find_ticket(
            visits, [ADMISSION_TICKET_NAME], ADMISSION_TICKET_ALT_NAMES
        )
        if admission:
            ok, _ = _process_ticket(admission, vn, date_str, label, before, findings)
            if ok:
                found_available = True
        else:
            print(f"  ⚪ {label} ({vn}p) — No Admission Ticket found")

        # Check extra tickets
        for et_name in EXTRA_TICKETS:
            et = _find_ticket(visits, [et_name], [])
            if et:
                ok, _ = _process_ticket(et, vn, date_str, label, before, findings)
                if ok:
                    found_available = True

    return found_available, has_data


def _format_findings(findings: list) -> str:
    """Build a detailed multi-line description of the available tickets."""
    if not findings:
        return "Admission Ticket available!"
    lines = []
    for f in findings:
        price = f" | €{f['price']}" if f.get("price") else ""
        header = (
            f"🎟️ {f['ticket']}\n"
            f"   {f['label']} | {f['visitors']}p"
            f" | {f['availability']}{price}"
        )
        lines.append(header)
        slots = f.get("slots") or []
        if slots:
            for s in slots[:8]:  # cap to avoid huge messages
                lines.append(
                    f"     • {s['time']} ({s['availability']},"
                    f" {s['residual']} left)"
                )
            if len(slots) > 8:
                lines.append(f"     • …and {len(slots) - 8} more slots")
        if f.get("deep_link"):
            lines.append(f"   👉 {f['deep_link']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _send_alerts(args, visitor_nums, dates, findings=None):
    """Send alerts via all configured channels."""
    title = "🎟️ Vatican Tickets!"
    if findings:
        msg = _format_findings(findings)
    else:
        fallback_link = _build_deep_link(visitor_nums[0], dates[0])
        msg = (
            f"Admission Ticket available!\n"
            f"Visitors: {visitor_nums}\n"
            f"Dates: {', '.join(dates)}\n\n"
            f"{fallback_link}"
        )

    if args.notify:
        notify(title, msg)

    if args.ntfy:
        notify_ntfy(args.ntfy, title, msg, priority="high")

    # Resolve Telegram creds same way as main()
    telegram_token = None
    telegram_chat_id = None
    if args.telegram:
        telegram_token, telegram_chat_id = args.telegram
    else:
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if telegram_token and telegram_chat_id:
        notify_telegram(telegram_token, telegram_chat_id, msg)


def main():
    parser = argparse.ArgumentParser(
        description="Vatican Museums Admission Ticket Monitor"
    )
    parser.add_argument(
        "--visitors", type=str, default="2",
        help="Comma-separated visitor counts (default: 2)"
    )
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Polling interval in seconds (default: 300 = 5 min)"
    )
    parser.add_argument(
        "--dates", type=str, default="30/05/2026,31/05/2026,01/06/2026",
        help="Dates to monitor, dd/MM/yyyy (default: May 30, 31 & June 1, 2026)"
    )
    parser.add_argument(
        "--before", type=str, default="10:30",
        help="Only show time slots before this time, HH:MM (default: 10:30). "
             "Set to 23:59 to show all."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run once and exit"
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send macOS desktop notification when available"
    )
    parser.add_argument(
        "--ntfy", type=str, metavar="TOPIC",
        help="Send push notification to phone via ntfy.sh topic (free, "
             "install ntfy app & subscribe to same topic)"
    )
    parser.add_argument(
        "--telegram", type=str, nargs=2, metavar=("BOT_TOKEN", "CHAT_ID"),
        help="Send notification via Telegram Bot (free)"
    )
    parser.add_argument(
        "--log", type=str, nargs="?", const="vatican_monitor.log",
        help="Write log to file (default: vatican_monitor.log)"
    )
    args = parser.parse_args()

    # Setup logging (tee print to file)
    if args.log:
        setup_log(args.log)

    visitor_nums = [int(v.strip()) for v in args.visitors.split(",")]
    dates = [d.strip() for d in args.dates.split(",")]

    # Resolve Telegram args — prefer CLI, fall back to env vars (for GitHub Actions)
    telegram_token = None
    telegram_chat_id = None
    if args.telegram:
        telegram_token, telegram_chat_id = args.telegram
    else:
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    print("=" * 58)
    print("  🏛️  VATICAN MUSEUMS — Admission Ticket Monitor")
    print("=" * 58)
    print(f"  Visitors : {visitor_nums}")
    print(f"  Dates    : {dates}")
    if args.before != "23:59":
        print(f"  Before   : {args.before} (afternoon excluded)")
    if args.once:
        print(f"  Mode     : Single check")
    else:
        print(f"  Interval : {args.interval}s")
    notif_parts = []
    if args.notify:
        notif_parts.append("Desktop")
    if args.ntfy:
        notif_parts.append(f"ntfy.sh/{args.ntfy}")
    if telegram_token and telegram_chat_id:
        notif_parts.append("Telegram")
    print(f"  Notify   : {', '.join(notif_parts) if notif_parts else 'OFF'}")
    print(f"  Time     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 58)

    # ─── Single check mode ───────────────────────────────────────────────
    if args.once:
        print()
        any_found = False
        findings = []
        for date_str in dates:
            found, _ = check_date(date_str, visitor_nums, args.before,
                                  findings=findings)
            if found:
                any_found = True

        print("\n" + "=" * 58)
        if any_found:
            print("  ✅ ADMISSION TICKETS AVAILABLE! 🎉")
            for f in findings:
                if f.get("deep_link"):
                    print(f"  👉 {f['label']} ({f['visitors']}p): {f['deep_link']}")
            _send_alerts(args, visitor_nums, dates, findings)
        else:
            print("  ❌ No admission tickets available.")
        print("=" * 58)
        return

    # ─── Polling loop ────────────────────────────────────────────────────
    check_num = 0
    while True:
        check_num += 1
        now = datetime.now()
        print(f"\n{'─' * 58}")
        print(f"  Check #{check_num} — {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'─' * 58}")

        any_available = False
        any_data = False
        findings = []

        for date_str in dates:
            found, has_data = check_date(date_str, visitor_nums, args.before,
                                         findings=findings)
            if found:
                any_available = True
            if has_data:
                any_data = True

        if any_available:
            msg = "🎉 Vatican Museums Admission Ticket AVAILABLE!"
            print(f"\n  {msg}")
            _send_alerts(args, visitor_nums, dates, findings)
        else:
            status = "SOLD OUT" if any_data else "API error"
            print(f"  📊 Status: {status}")

        next_check = datetime.fromtimestamp(time.time() + args.interval)
        print(f"  ⏱️  Next check: {next_check.strftime('%H:%M:%S')} "
              f"(in {args.interval}s)")

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Monitor stopped.")
        sys.exit(0)
