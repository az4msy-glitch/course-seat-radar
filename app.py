import os
import time
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")

# --------- CONFIG FROM ENV ----------
TERM = os.environ.get("TERM_CODE", "252")
DEPTS = [d.strip() for d in os.environ.get("DEPTS", "EE").split(",") if d.strip()]
CRNS = [c.strip() for c in os.environ.get("CRNS", "").split(",") if c.strip()]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Remember which CRNs we already notified about (so we don't spam)
NOTIFIED = set()


# --------- TELEGRAM ----------
def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("BOT_TOKEN or CHAT_ID missing, cannot send Telegram.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        if r.status_code != 200:
            logging.error(f"Telegram error {r.status_code}: {r.text}")
        else:
            logging.info("Telegram message sent.")
    except Exception as e:
        logging.error(f"Telegram request failed: {e}")


# --------- FREE-COURSES API ----------
def fetch_department(term: str, dept: str):
    """Get all courses for a department (EE, ENGL, …)."""
    url = f"https://free-courses.dev/api/courses?term={term}&course={dept}"
    logging.info(f"[DEBUG] Fetching department data: {url}")

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data
    except Exception as e:
        logging.error(f"[ERROR] Dept {dept}: {e}")
        return None


def run_radar():
    logging.info("=== Seat Radar Running ===")
    logging.info(f"Term: {TERM}")
    logging.info(f"Departments: {DEPTS}")
    logging.info(f"Tracking CRNs: {CRNS}")

    while True:
        logging.info(f"[INFO] Checking at {time.strftime('%Y-%m-%d %H:%M:%S')}")

        for dept in DEPTS:
            dept = dept.strip()
            if not dept:
                continue

            courses = fetch_department(TERM, dept)
            if not courses:
                continue

            for course in courses:
                crn = str(course.get("crn", "")).strip()
                if crn not in CRNS:
                    continue

                title = course.get("title", "Unknown")
                section = course.get("section", "")
                # free-courses.dev usually exposes these fields:
                # available, capacity, enrolled  (all integers)
                available = course.get("available")
                capacity = course.get("capacity")
                enrolled = course.get("enrolled")

                logging.info(
                    f"[DEBUG] {dept} CRN {crn}: avail={available}, "
                    f"cap={capacity}, enrolled={enrolled}"
                )

                if available is None or capacity is None:
                    # if API doesn't give us those numbers, skip
                    continue

                if available > 0:
                    # send only once per CRN until you restart the service
                    if crn not in NOTIFIED:
                        msg = (
                            "🎉 Seat Available!\n\n"
                            f"Department: {dept}\n"
                            f"Course: {title} ({section})\n"
                            f"CRN: {crn}\n"
                            f"Available: {available}\n"
                            f"Capacity: {capacity}\n"
                            f"Enrolled: {enrolled}\n"
                            f"Term: {TERM}"
                        )
                        send_telegram(msg)
                        NOTIFIED.add(crn)
                else:
                    logging.info(f"[INFO] CRN {crn}: no seats ({available}/{capacity}).")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_radar()
