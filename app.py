import os
import time
import json
from datetime import datetime

import requests

print("DEBUG: APP STARTED")

# =============================
# CONFIGURATION
# =============================

TERM = 252  # KFUPM term

# CRNs you want to track
COURSES = {
    "EE207-02": 22716,
    "EE271-53": 22425,
    "EE272-57": 22436,
    "ENGL214-14": 20305,
}

# NEW WORKING API ENDPOINT
API_URL = "https://free-courses.dev/api/courses/crn"

# Retrieve from Render environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

STATUS_FILE = "course_status.json"


# =============================
# TELEGRAM SEND FUNCTION
# =============================

def send_telegram(message: str):
    """Send Telegram alert."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN or CHAT_ID not set!")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}

    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        print("[INFO] Telegram sent")
    except Exception as e:
        print("[ERROR] Telegram failed:", e)


# =============================
# STATE SAVE / LOAD
# =============================

def load_status():
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_status(s):
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        print("[ERROR] Could not save status:", e)


# =============================
# CHECK ONE CRN FROM API
# =============================

def check_crn(crn: int):
    """Query API for a CRN using safe endpoint."""
    url = f"https://free-courses.dev/api/courses/crn/{TERM}/{crn}"
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
        print(f"[ERROR] API error for CRN {crn}:", e)
        return None


# =============================
# MAIN RADAR LOOP
# =============================

def run_radar():
    print("=== Seat Radar Started (Cloud Server Mode) ===")

    status = load_status()

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[INFO] Checking at {now}")

        for label, crn in COURSES.items():
            info = check_crn(crn)

            if not info:
                continue

            available = info["available"]
            prev = status.get(str(crn), -1)

            print(f"{label} (CRN {crn}) → Available: {available}")

            # Trigger alert only when available goes from 0 → >0
            if available is not None and available > 0 and prev == 0:
                msg = (
                    f"📢 SEAT AVAILABLE!\n\n"
                    f"Course: {label}\n"
                    f"CRN: {crn}\n"
                    f"Available: {available}\n"
                    f"Enrolled: {info['enrolled']}/{info['capacity']}\n"
                    f"REGISTER NOW!"
                )
                send_telegram(msg)

            status[str(crn)] = available

        save_status(status)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_radar()
