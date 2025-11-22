import os
import time
import requests
from typing import Dict, Any, List

# ============================================================
# CONFIG FROM ENVIRONMENT (Render or local)
# ============================================================

# Term code, e.g. "252"
TERM = os.environ.get("TERM_CODE", "252").strip()

# CRNs list, e.g. "22716,22425,22436,20305"
_raw_crns = os.environ.get("CRNS", "22716,22425,22436,20305")
CRNS: List[int] = []
for part in _raw_crns.replace(";", ",").split(","):
    part = part.strip()
    if part.isdigit():
        CRNS.append(int(part))

# Check interval (seconds)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))

# Telegram credentials
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

API_BASE = "https://api.free-courses.dev/courses/crn"

# ============================================================


def log(msg: str) -> None:
    """Simple logger that always flushes (handy on Render)."""
    print(msg, flush=True)


def send_telegram(text: str) -> None:
    """Send a message via Telegram Bot API."""
    if not BOT_TOKEN or not CHAT_ID:
        log("[ERROR] BOT_TOKEN or CHAT_ID not set – cannot send Telegram.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        r.raise_for_status()
        log("[INFO] Telegram message sent.")
    except Exception as e:
        log(f"[ERROR] Telegram failed: {e}")


def fetch_crn_info(term: str, crn: int) -> Dict[str, Any]:
    """
    Call the free-courses.dev CRN endpoint and return the JSON data.

    Expected JSON fields (as seen in your browser):
      - course   (e.g. 'EE207')
      - section  (e.g. '02')
      - available (int)
      - capacity  (int)
      - enrolled  (int)
    """
    url = f"{API_BASE}?term={term}&crn={crn}"
    log(f"[DEBUG] Requesting: {url}")

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()

        # Make sure we got JSON, not HTML
        content_type = r.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            log(f"[ERROR] CRN {crn}: Non-JSON response (Content-Type={content_type})")
            return {}

        data = r.json()
        if not isinstance(data, dict):
            log(f"[ERROR] CRN {crn}: JSON root is not an object: {data!r}")
            return {}

        return data

    except Exception as e:
        log(f"[ERROR] CRN {crn}: request/json error: {e}")
        return {}


def run_radar() -> None:
    log("=== Seat Radar Running (Render) ===")
    log(f"Term: {TERM}")
    log(f"CRNs: {CRNS}")
    log(f"Check interval: {CHECK_INTERVAL} seconds")

    if not CRNS:
        log("[WARN] No valid CRNs parsed from CRNS env var. Exiting.")
        return

    # remembers which CRNs we already alerted on (to avoid spam)
    notified: Dict[int, int] = {}

    while True:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        log(f"[INFO] Checking at {now}")

        for crn in CRNS:
            info = fetch_crn_info(TERM, crn)
            if not info:
                continue

            available = info.get("available")
            capacity = info.get("capacity")
            enrolled = info.get("enrolled")
            course = info.get("course", "Unknown")
            section = info.get("section", "")

            log(f"[DEBUG] CRN {crn}: available={available}, capacity={capacity}, enrolled={enrolled}")

            # Must be proper ints to make a decision
            if not isinstance(available, int) or not isinstance(capacity, int):
                log(f"[WARN] CRN {crn}: missing/invalid availability numbers, skipping.")
                continue

            # If there are seats available
            if available > 0:
                # Only notify if it's the first time or the available count changed
                last_notified_avail = notified.get(crn)
                if last_notified_avail != available:
                    msg = (
                        "🎉 *Seat Available!*\n\n"
                        f"*Course:* {course} ({section})\n"
                        f"*CRN:* `{crn}`\n"
                        f"*Available:* {available}\n"
                        f"*Capacity:* {capacity}\n"
                        f"*Enrolled:* {enrolled}\n"
                        f"*Term:* {TERM}"
                    )
                    send_telegram(msg)
                    notified[crn] = available
            else:
                log(f"[INFO] CRN {crn}: no seats ({available}/{capacity})")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_radar()
