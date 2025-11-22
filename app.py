import os
import time
import requests
import logging
from datetime import datetime

# --------------------------
# Logging Setup
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

print("DEBUG: APP STARTED")
print("=== Seat Radar Started (Cloud Server Mode) ===")

# --------------------------
# Environment Variables
# --------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

# IMPORTANT: renamed variable to avoid system conflict
TERM = int(os.environ.get("COURSE_TERM", "252"))

# List of CRNs to monitor
CRNS = [22716, 22425, 22436, 20305]   # EE207, EE271, EE272, ENGL214

# Tracks seat status
last_status = {crn: None for crn in CRNS}


# --------------------------
# Telegram Notification
# --------------------------
def send_telegram_message(msg: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": msg}
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        logging.error(f"[ERROR] Failed to send Telegram message: {e}")


# --------------------------
# Check CRN Seats
# --------------------------
def check_crn(term: int, crn: int):
    """Query free-courses.dev API for seat availability."""
    url = f"https://free-courses.dev/api/courses/crn?term={term}&crn={crn}"

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        return {
            "available": data.get("available"),
            "capacity": data.get("capacity"),
            "enrolled": data.get("enrolled"),
            "course": data.get("course"),
            "section": data.get("section"),
        }

    except Exception as e:
        logging.error(f"[ERROR] API error for CRN {crn}: {e}")
        return None


# --------------------------
# Radar Loop
# --------------------------
def run_radar():
    logging.info("Radar started...")

    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"[INFO] Checking at {timestamp}")

        for crn in CRNS:
            info = check_crn(TERM, crn)

            if info is None:
                continue

            available = info["available"]
            enrolled = info["enrolled"]
            capacity = info["capacity"]
            course = info["course"]
            section = info["section"]

            # Only notify if seat count changed
            if last_status[crn] != available:
                last_status[crn] = available

                if available > 0:
                    msg = (
                        f"🎉 Seat Available!\n"
                        f"Course: {course} (Sec {section})\n"
                        f"CRN: {crn}\n\n"
                        f"Available Seats: {available}\n"
                        f"Enrolled: {enrolled}/{capacity}\n"
                        f"Term: {TERM}"
                    )
                    send_telegram_message(msg)

                logging.info(
                    f"[INFO] CRN {crn} updated (Available = {available})"
                )

        time.sleep(CHECK_INTERVAL)


# --------------------------
# Start
# --------------------------
if __name__ == "__main__":
    run_radar()
