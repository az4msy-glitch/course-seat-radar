import requests
import time
import os
import logging
from datetime import datetime
import json

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger.__name__

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('CHAT_ID')
CHECK_INTERVAL = 2  # 2 seconds ⚡ (as you requested!)
WEBSITE_EMAIL = os.getenv('WEBSITE_EMAIL')
WEBSITE_PASSWORD = os.getenv('WEBSITE_PASSWORD')

# API Endpoints
LOGIN_URL = "https://api.free-courses.dev/auth/login"
COURSES_URL = "https://api.free-courses.dev/courses"

# Courses to monitor
COURSES_TO_MONITOR = {
    "EE": [
        {"code": "EE207", "section": "02", "crn": "22716"},
        {"code": "EE271", "section": "53", "crn": "20825"},
        {"code": "EE272", "section": "57", "crn": "20830"}
    ],
    "ENGL": [
        {"code": "ENGL214", "section": "14", "crn": "21510"}
    ]
}

# Global session to reuse
current_session = None
last_login_time = 0
SESSION_DURATION = 1800  # 30 minutes before relogin

def send_telegram_message(message):
    """Send message to Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=data)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def login_to_website():
    """Login to the course website and return session"""
    global current_session, last_login_time
    
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'Origin': 'https://free-courses.dev',
            'Referer': 'https://free-courses.dev/'
        })
        
        # Login
        login_data = {"email": WEBSITE_EMAIL, "password": WEBSITE_PASSWORD}
        response = session.post(LOGIN_URL, json=login_data)
        
        if response.status_code == 200:
            token = response.json().get('token')
            if token:
                session.headers.update({'Authorization': f'Bearer {token}'})
                current_session = session
                last_login_time = time.time()
                logger.info("✅ Successfully logged in")
                return session
        else:
            logger.error(f"❌ Login failed: {response.status_code} - {response.text}")
        return None
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return None

def get_session():
    """Get current session or login if needed"""
    global current_session, last_login_time
    
    # Check if we need a new session
    if (current_session is None or 
        time.time() - last_login_time > SESSION_DURATION):
        logger.info("🔄 Session expired or not exists, logging in...")
        return login_to_website()
    
    return current_session

def get_department_courses(department):
    """Get courses for a specific department"""
    session = get_session()
    if not session:
        return []
    
    try:
        params = {
            "term": "252",
            "course": department
        }
        
        response = session.get(COURSES_URL, params=params)
        
        if response.status_code == 200:
            try:
                courses_data = response.json()
                if isinstance(courses_data, list):
                    logger.info(f"✅ Got {len(courses_data)} courses for {department}")
                return courses_data
            except json.JSONDecodeError:
                logger.info(f"Response is not JSON for {department}")
                return []
        elif response.status_code == 401:
            # Token expired, force relogin
            logger.warning("🔄 Token expired, forcing relogin...")
            global current_session
            current_session = None
            return []
        else:
            logger.error(f"❌ Failed to get {department} courses: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Error getting {department} courses: {e}")
        return []

def check_course_availability():
    """Check availability for all monitored courses"""
    try:
        all_available_courses = []
        
        # Check each department
        for department, courses in COURSES_TO_MONITOR.items():
            department_courses = get_department_courses(department)
            
            if not department_courses:
                continue
            
            # If we have a list of courses, process them
            if isinstance(department_courses, list):
                # Find our specific courses in the department results
                for target_course in courses:
                    found_course = None
                    
                    # Search for the course in the department results
                    for course in department_courses:
                        # Handle dictionary responses
                        if isinstance(course, dict):
                            course_code = course.get('code', '')
                            section = course.get('section', '')
                            crn = course.get('crn', '')
                            seats = course.get('seats', '')
                            
                            # Match by course code + section, or by CRN
                            matches_code = (course_code == target_course['code'] and 
                                          section == target_course['section'])
                            matches_crn = crn == target_course['crn']
                            
                            if matches_code or matches_crn:
                                found_course = course
                                break
                    
                    if found_course:
                        # Check seat availability
                        seats = found_course.get('seats', '')
                        if seats and '/' in str(seats):
                            try:
                                current_seats, total_seats = str(seats).split('/')
                                available_seats = int(current_seats.strip())
                                if available_seats > 0:
                                    course_info = {
                                        'department': department,
                                        'code': target_course['code'],
                                        'section': target_course['section'],
                                        'crn': found_course.get('crn', 'N/A'),
                                        'title': found_course.get('title', 'N/A'),
                                        'instructor': found_course.get('instructor', 'N/A'),
                                        'schedule': f"{found_course.get('days', 'N/A')} {found_course.get('time', 'N/A')}",
                                        'seats': seats,
                                        'available_seats': available_seats,
                                        'location': found_course.get('location', 'N/A')
                                    }
                                    all_available_courses.append(course_info)
                                    logger.info(f"✅ Available: {department} {target_course['code']}-{target_course['section']} - {seats}")
                            except (ValueError, AttributeError) as e:
                                logger.error(f"Error parsing seats for {target_course['code']}: {e}")
        
        return all_available_courses
        
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return []

def monitor_loop():
    """Main monitoring loop"""
    logger.info("🚀 Starting course availability monitor...")
    
    # Send startup message
    courses_list = []
    for department, courses in COURSES_TO_MONITOR.items():
        for course in courses:
            courses_list.append(f"• {course['code']}-{course['section']} (CRN: {course['crn']})")
    
    startup_message = f"""🤖 <b>Course Monitor Started!</b>

<b>Monitoring Courses:</b>
{"\n".join(courses_list)}

<b>Term:</b> 252
<b>Check Interval:</b> Every {CHECK_INTERVAL} seconds ⚡
<b>Session Reuse:</b> ✅ Enabled
<b>Status:</b> 🟢 ACTIVE"""

    send_telegram_message(startup_message)
    
    previous_available = set()
    check_count = 0
    
    while True:
        try:
            check_count += 1
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"🔍 Check #{check_count} at {current_time}")
            
            available_courses = check_course_availability()
            
            # Create unique identifiers
            current_identifiers = set()
            for course in available_courses:
                identifier = f"{course['code']}-{course['section']}-{course['crn']}"
                current_identifiers.add(identifier)
            
            # Find newly available courses
            new_courses = current_identifiers - previous_available
            
            if new_courses:
                message = f"🎉 <b>COURSES AVAILABLE!</b> 🎉\n\n"
                
                for course in available_courses:
                    identifier = f"{course['code']}-{course['section']}-{course['crn']}"
                    if identifier in new_courses:
                        message += f"✅ <b>{course['code']}-{course['section']}</b> ({course['department']})\n"
                        message += f"   📚 {course['title']}\n"
                        message += f"   👨‍🏫 {course['instructor']}\n"
                        message += f"   🕒 {course['schedule']}\n"
                        message += f"   📍 {course['location']}\n"
                        message += f"   🪑 Seats: <b>{course['seats']}</b>\n"
                        message += f"   🔢 CRN: {course['crn']}\n\n"
                
                message += f"🕒 {current_time}"
                
                if send_telegram_message(message):
                    logger.info(f"📤 Sent notification for {len(new_courses)} courses")
                else:
                    logger.error("❌ Failed to send notification")
            else:
                if available_courses:
                    logger.info(f"📊 Courses available but no new ones: {len(available_courses)} courses")
                else:
                    logger.info("📊 No courses available")
            
            # Update previous state
            previous_available = current_identifiers
            
            logger.info(f"✅ Check #{check_count} completed. Found {len(available_courses)} available courses")
            logger.info(f"⏰ Waiting {CHECK_INTERVAL} seconds for next check...")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"❌ Monitor error: {e}")
            logger.info("⏰ Waiting 10 seconds before retrying...")
            time.sleep(10)

if __name__ == "__main__":
    # Validate environment
    required_vars = ['BOT_TOKEN', 'CHAT_ID', 'WEBSITE_EMAIL', 'WEBSITE_PASSWORD']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    logger.info(f"🔧 Configuration: Check interval: {CHECK_INTERVAL}s")
    monitor_loop()
