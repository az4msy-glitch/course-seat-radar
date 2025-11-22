import os
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

# Load environment variables
TERM = os.environ.get("TERM_CODE", "252")
DEPTS = os.environ.get("DEPTS", "EE").split(",")
CRNS = [c.strip() for c in os.environ.get("CRNS", "").split(",")]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 60))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

NOTIFIED = set()

def telegram(message: str):
    """Send a Telegram message."""
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Telegram credentials missing.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": message})
    except:
        pass


def check_crn(crn: str):
    """Query API for a CRN."""
    url = f"https://free-courses.dev/api/courses?term={TERM}&crn={crn}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data
    except Exception as e:
        logging.error(f"[ERROR] CRN {crn}: {e}")
        return None


def run_radar():
    logging.info("=== Seat Radar Running ===")
    logging.info(f"Monitoring CRNs: {CRNS}")
    logging.info(f"Departments: {DEPTS}")
    logging.info(f"Term: {TERM}")

    while True:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"[INFO] Checking at {now}")

        for crn in CRNS:
            info = check_crn(crn)
            if not info:
                continue

            available = info.get("available")
            capacity = info.get("capacity")
            title = info.get("title", "Unknown")
            section = info.get("section", "")

            if available is None:
                continue

            if available > 0:
                if crn not in NOTIFIED:
                    msg = (
                        f"🎉 Seat Available!\n"
                        f"CRN: {crn}\n"
                        f"Course: {title} ({section})\n"
                        f"Seats: {available}/{capacity}"
                    )
                    telegram(msg)
                    NOTIFIED.add(crn)
            else:
                logging.info(f"CRN {crn} → 0 seats")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    logging.info("DEBUG: Starting radar…")
    run_radar()
