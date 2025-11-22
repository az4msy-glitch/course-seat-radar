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
TERM = int(os.environ.get("TERM", "252"))

# CRNs to Monitor
CRNS = [22716, 22425, 22436, 20305]   # EE207, EE271, EE272, ENGL214

# Last known seat counts
last_status = {crn: None for crn in CRNS}


# --------------------------
# Telegram Notification
# --------------------------
def send_telegram_message(message: str):
    """Send message to Telegram bot."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"[ERROR] Failed to send Telegram message: {e}")


# --------------------------
# CRN Seat Checker
# --------------------------
def check_crn(term: int, crn: int):
    """Query the API for a CRN seat availability."""
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
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"[INFO] Checking at {now}")

        for crn in CRNS:
            info = check_crn(TERM, crn)

            if info is None:
                continue  # Skip if API failed

            available = info["available"]
            capacity = info["capacity"]
            enrolled = info["enrolled"]
            course = info["course"]
            section = info["section"]

            # Detect change in availability
            if last_status[crn] != available:
                last_status[crn] = available

                if available > 0:
                    msg = (
                        f"🎉 Seat Available!\n\n"
                        f"Course: {course} (Section {section})\n"
                        f"CRN: {crn}\n"
                        f"Available Seats: {available}\n"
                        f"Enrolled: {enrolled}/{capacity}\n"
                        f"Term: {TERM}"
                    )
                    send_telegram_message(msg)
                    logging.info(f"[INFO] Seat opened for CRN {crn}")

                else:
                    logging.info(
                        f"[INFO] CRN {crn} updated but still full (Available = {available})"
                    )

        time.sleep(CHECK_INTERVAL)


# --------------------------
# Start Radar
# --------------------------
if __name__ == "__main__":
    run_radar()
