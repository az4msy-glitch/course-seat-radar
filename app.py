import os
import time
import requests

# ===== Read environment variables =====
TERM = os.environ.get("TERM", "252")
DEPTS = os.environ.get("DEPTS", "EE,ENGL").split(",")
CRNS = [c.strip() for c in os.environ.get("CRNS", "").split(",")]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


# ===== Telegram Sender =====
def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] No BOT_TOKEN or CHAT_ID found.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": message})
        print("[INFO] Telegram message sent")
    except Exception as e:
        print("[ERROR] Failed to send Telegram message:", e)


# ===== API Call for CRN + Department =====
def check_course(term, dept, crn):
    """
    Query Free-Courses API using dept endpoint.
    """
    url = f"https://free-courses.dev/api/courses?term={term}&course={dept}"
    print(f"[DEBUG] Fetching: {url}")

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Look for target CRN inside department list
        for course in data:
            if str(course.get("crn")) == str(crn):
                return {
                    "course": course.get("title"),
                    "crn": crn,
                    "available": course.get("available"),
                    "capacity": course.get("capacity"),
                    "enrolled": course.get("enrolled"),
                    "dept": dept
                }
        return None

    except Exception as e:
        print(f"[ERROR] API error for dept {dept}, CRN {crn}: {e}")
        return None


# ===== Main Radar Loop =====
def run_radar():
    print("=== Seat Radar Started ===")
    print(f"Tracking CRNs: {CRNS}")
    print(f"Departments: {DEPTS}")

    while True:
        print(f"[INFO] Checking at {time.strftime('%Y-%m-%d %H:%M:%S')}")

        for crn in CRNS:
            for dept in DEPTS:
                info = check_course(TERM, dept, crn)
                if not info:
                    continue

                if info["available"] is None:
                    # means API didn't provide seats info, ignore
                    continue

                available = info["available"]
                capacity = info["capacity"]
                enrolled = info["enrolled"]

                print(f"[DEBUG] CRN {crn}: {available}/{capacity}, enrolled={enrolled}")

                if available > 0:
                    msg = (
                        f"🎉 Seat Available!\n\n"
                        f"Course: {info['course']} ({info['dept']})\n"
                        f"CRN: {info['crn']}\n"
                        f"Seats: {available}/{capacity}\n"
                        f"Enrolled: {enrolled}\n"
                        f"Term: {TERM}"
                    )
                    send_telegram(msg)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_radar()
