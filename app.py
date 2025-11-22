import os
import time
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

TERM_CODE = os.environ.get("TERM_CODE", "252")
DEPTS = os.environ.get("DEPTS", "EE").split(",")
CRNS = os.environ.get("CRNS", "").split(",")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))


def notify(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg})
    except:
        pass


def fetch_by_crn(term, crn):
    url = f"https://free-courses.dev/api/courses/crn?term={term}&crn={crn}"
    r = requests.get(url)
    if r.status_code != 200:
        print(f"[ERROR] CRN {crn}: HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except:
        print(f"[ERROR] CRN {crn}: invalid JSON")
        return None


def fetch_by_dept(term, dept):
    url = f"https://free-courses.dev/api/courses?term={term}&course={dept}"
    r = requests.get(url)
    if r.status_code != 200:
        print(f"[ERROR] Dept {dept}: HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except:
        print(f"[ERROR] Dept {dept}: invalid JSON")
        return None


def run():
    print("=== Seat Radar Running ===")
    print("Term:", TERM_CODE)
    print("Departments:", DEPTS)
    print("CRNS:", CRNS)

    while True:
        print("[INFO] Checking…")

        # 1 — Check CRNs directly
        for crn in CRNS:
            crn = crn.strip()
            if not crn:
                continue

            data = fetch_by_crn(TERM_CODE, crn)
            if not data:
                continue

            seats = data.get("seats")
            if seats and " / " in seats:
                enrolled, capacity = map(int, seats.split(" / "))
                if enrolled < capacity:
                    notify(f"Seat OPEN for CRN {crn}! Seats: {seats}")

        # 2 — Check departments (for English + EE)
        for dept in DEPTS:
            dept = dept.strip()
            if not dept:
                continue

            data = fetch_by_dept(TERM_CODE, dept)
            if not data:
                continue

            for course in data:
                seats = course.get("seats", "")
                crn = course.get("crn")
                if seats and " / " in seats:
                    enrolled, capacity = map(int, seats.split(" / "))
                    if enrolled < capacity:
                        notify(f"[{dept}] Seat OPEN in course CRN {crn}! Seats: {seats}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
