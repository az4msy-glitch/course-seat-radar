import os
import time
import logging
from datetime import datetime

import requests

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

print("DEBUG: APP STARTED")
print("=== KFUPM Seat Radar (EE + ENGL214) ===")

# --------------------------
# Config (edit here if needed)
# --------------------------
TERM = 252           # current term
COURSE_CODE = "EE"   # department code used by free-courses.dev

# CRNs you want to watch
WATCHED_CRNS = {
    22716: "EE207-02",
    22425: "EE271-53",
    22436: "EE272-57",
    20305: "ENGL214-14",
}

API_URL = "https://free-courses.dev/api/courses"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # seconds

# last known "available" seats per CRN
last_status = {crn: None for crn in WATCHED_CRNS.keys()}


# --------------------------
# Helpers
# --------------------------
def send_telegram_message(text: str) -> None:
    """Send a message via Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("BOT_TOKEN or CHAT_ID not set, cannot send Telegram message.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        logging.info("Telegram message sent.")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")


def fetch_ee_courses():
    """
    Call free-courses.dev to get ALL EE courses for the term.
    We use the same request you saw in DevTools:
    courses?term=252&course=EE
    """
    params = {"term": TERM, "course": COURSE_CODE}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    }
    try:
        r = requests.get(API_URL, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()

        # Sometimes the API returns {"courses": [ ... ]}, sometimes just [ ... ]
        if isinstance(data, dict) and "courses" in data:
            return data["courses"]
        elif isinstance(data, list):
            return data
        else:
            logging.warning(f"Unexpected API response shape: {type(data)}")
            return []
    except Exception as e:
        logging.error(f"Error fetching courses: {e}")
        return []


def extract_available(info: dict):
    """
    Try to figure out available seats from the JSON.
    We handle several possible formats to be safe.
    """
    # 1) direct field
    if "available" in info:
        return info["available"], info.get("capacity"), info.get("enrolled")

    # 2) "seats" like "21/22"
    seats_str = info.get("seats") or info.get("SEATS")
    if isinstance(seats_str, str) and "/" in seats_str:
        try:
            enrolled_str, capacity_str = seats_str.split("/", 1)
            enrolled = int(enrolled_str.strip())
            capacity = int(capacity_str.strip())
            available = capacity - enrolled
            return available, capacity, enrolled
        except Exception:
            pass

    # 3) separate fields
    if "capacity" in info and "enrolled" in info:
        try:
            capacity = int(info["capacity"])
            enrolled = int(info["enrolled"])
            available = capacity - enrolled
            return available, capacity, enrolled
        except Exception:
            pass

    # if we fail, just return None
    return None, None, None


# --------------------------
# Main Radar Loop
# --------------------------
def run_radar():
    logging.info("Seat radar started.")

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"Checking at {now} ...")

        courses = fetch_ee_courses()
        logging.info(f"Fetched {len(courses)} EE courses from API.")

        # Build a fast lookup: CRN -> course info
        by_crn = {}
        for c in courses:
            crn_raw = c.get("crn") or c.get("CRN")
            if crn_raw is None:
                continue
            try:
                crn_int = int(crn_raw)
            except Exception:
                continue
            by_crn[crn_int] = c

        # Check only the CRNs we care about
        for crn, label in WATCHED_CRNS.items():
            info = by_crn.get(crn)
            if not info:
                logging.warning(f"CRN {crn} ({label}) not found in API result.")
                continue

            available, capacity, enrolled = extract_available(info)

            logging.info(
                f"{label} (CRN {crn}) -> Available: {available}, "
                f"Enrolled: {enrolled}, Capacity: {capacity}"
            )

            prev = last_status.get(crn)

            # First run: just store value
            if prev is None:
                last_status[crn] = available
                continue

            # If availability went from 0 (or None) to >0 -> alert!
            if available is not None and available > 0 and (prev is None or prev <= 0):
                msg = (
                    "📢 SEAT AVAILABLE!\n\n"
                    f"Course: {label}\n"
                    f"CRN: {crn}\n"
                    f"Available: {available}\n"
                    f"Enrolled: {enrolled}/{capacity}\n"
                    f"Term: {TERM}\n\n"
                    "Go register NOW in KFUPM portal."
                )
                send_telegram_message(msg)

            # Update status
            last_status[crn] = available

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_radar()
