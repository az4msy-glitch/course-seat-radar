import requests
import time
import os
import logging
from datetime import datetime
import json
import threading

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('CHAT_ID')
CHECK_INTERVAL = 5  # 5 seconds ⚡
WEBSITE_EMAIL = os.getenv('WEBSITE_EMAIL')
WEBSITE_PASSWORD = os.getenv('WEBSITE_PASSWORD')

# API Endpoints
LOGIN_URL = "https://api.free-courses.dev/auth/login"
COURSES_URL = "https://api.free-courses.dev/courses"

# Courses storage
COURSES_FILE = "monitored_courses.json"

# Smart rate limiting
current_session = None
last_login_time = 0
last_api_call_time = 0
SESSION_DURATION = 1800  # 30 minutes
MIN_API_INTERVAL = 3     # 3 seconds between API calls to same department
last_department_call = {}  # Track last call time per department

def load_courses():
    """Load monitored courses from file"""
    try:
        if os.path.exists(COURSES_FILE):
            with open(COURSES_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading courses: {e}")
    
    # Default courses
    return {
        "EE": [
            {"code": "EE207", "section": "02", "crn": "22716"},
            {"code": "EE271", "section": "53", "crn": "20825"},
            {"code": "EE272", "section": "57", "crn": "20830"}
        ],
        "ENGL": [
            {"code": "ENGL214", "section": "14", "crn": "21510"}
        ]
    }

def save_courses(courses_data):
    """Save monitored courses to file"""
    try:
        with open(COURSES_FILE, 'w') as f:
            json.dump(courses_data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving courses: {e}")
        return False

def send_telegram_message(message, chat_id=None):
    """Send message to Telegram"""
    try:
        if chat_id is None:
            chat_id = TELEGRAM_CHAT_ID
            
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        response = requests.post(url, data=data)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def smart_rate_limit(department):
    """Smart rate limiting per department - PREVENTATIVE"""
    global last_department_call
    current_time = time.time()
    
    if department in last_department_call:
        time_since_last_call = current_time - last_department_call[department]
        if time_since_last_call < 10:  # Don't call if within 10 seconds
            # 🎯 NO SLEEP - just skip and let the main function handle it
            return
    
    # Only update if we're actually making the call
    last_department_call[department] = time.time()

def login_to_website():
    """Login to the course website"""
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
            logger.error(f"❌ Login failed: {response.status_code}")
        return None
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return None

def get_session():
    """Get current session or login if needed"""
    global current_session, last_login_time
    
    if (current_session is None or 
        time.time() - last_login_time > SESSION_DURATION):
        logger.info("🔄 Session expired, logging in...")
        return login_to_website()
    
    return current_session

def get_department_courses(department):
    """Get courses for a specific department with smart rate limiting"""
    session = get_session()
    if not session:
        return []
    
    try:
        # 🎯 SMART RATE LIMIT CHECK - Skip if we recently hit limit
        current_time = time.time()
        if department in last_department_call:
            time_since_last_call = current_time - last_department_call[department]
            if time_since_last_call < 10:  # Skip if called in last 10 seconds
                logger.info(f"⏭️ Skipping {department} - rate limit cooldown")
                return []
        
        # Smart rate limiting per department
        smart_rate_limit(department)
        
        params = {"term": "252", "course": department}
        
        logger.info(f"📡 Fetching {department} courses...")
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
        elif response.status_code == 429:
            logger.warning(f"⚠️ Rate limited for {department}. Will skip next check.")
            # 🎯 SET COOLDOWN - Skip this department for 15 seconds
            last_department_call[department] = time.time()
            return []
        elif response.status_code == 401:
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
        courses_data = load_courses()
        
        # Check each department
        for department, courses in courses_data.items():
            if not courses:
                continue
                
            department_courses = get_department_courses(department)
            
            if not department_courses:
                continue
            
            # Find our specific courses
            if isinstance(department_courses, list):
                for target_course in courses:
                    found_course = None
                    
                    for course in department_courses:
                        if isinstance(course, dict):
                            course_code = course.get('code', '')
                            section = course.get('section', '')
                            crn = course.get('crn', '')
                            seats = course.get('seats', '')
                            
                            matches_code = (course_code == target_course['code'] and 
                                          section == target_course['section'])
                            matches_crn = crn == target_course['crn']
                            
                            if matches_code or matches_crn:
                                found_course = course
                                break
                    
                    if found_course:
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
                                    logger.info(f"🎯 AVAILABLE: {department} {target_course['code']}-{target_course['section']} - {seats}")
                            except (ValueError, AttributeError) as e:
                                logger.error(f"Error parsing seats for {target_course['code']}: {e}")
        
        return all_available_courses
        
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return []

# Telegram Commands (keep all the interactive features)
def handle_telegram_commands():
    """Handle incoming Telegram commands"""
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            response = requests.get(url, params=params)
            
            if response.status_code == 200:
                data = response.json()
                if data["ok"] and data["result"]:
                    for update in data["result"]:
                        last_update_id = update["update_id"]
                        message = update.get("message", {})
                        chat_id = message.get("chat", {}).get("id")
                        text = message.get("text", "").strip()
                        
                        if chat_id == int(TELEGRAM_CHAT_ID) and text:
                            process_command(text, chat_id)
            
            time.sleep(1)
        except Exception as e:
            logger.error(f"Telegram commands error: {e}")
            time.sleep(5)

def process_command(text, chat_id):
    """Process Telegram commands"""
    text_lower = text.lower()
    
    if text_lower == "/start":
        send_welcome_message(chat_id)
    elif text_lower == "/status":
        send_status(chat_id)
    elif text_lower == "/courses":
        send_monitored_courses(chat_id)
    elif text_lower == "/addcourse":
        send_telegram_message("📝 To add a course:\n<code>/add COURSE-SECTION CRN</code>\nExample: <code>/add EE207-02 22716</code>", chat_id)
    elif text_lower.startswith("/add "):
        add_course(text, chat_id)
    elif text_lower.startswith("/remove "):
        remove_course(text, chat_id)
    elif text_lower == "/help":
        send_help(chat_id)
    else:
        send_telegram_message("❓ Use /help for commands", chat_id)

def send_welcome_message(chat_id):
    message = """🤖 <b>Course Monitor Bot</b>

<b>Commands:</b>
/status - Bot status
/courses - Show courses  
/addcourse - How to add
/remove [course] - Remove
/help - All commands

<b>Check Interval:</b> 5 seconds ⚡"""
    send_telegram_message(message, chat_id)

def send_status(chat_id):
    courses_data = load_courses()
    total_courses = sum(len(courses) for courses in courses_data.values())
    
    message = f"""📊 <b>Bot Status</b>

<b>Monitoring:</b> {total_courses} courses
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Departments:</b> {', '.join(courses_data.keys())}
<b>Status:</b> 🟢 ACTIVE
<b>Rate Limit:</b> Smart prevention enabled"""
    send_telegram_message(message, chat_id)

def send_monitored_courses(chat_id):
    courses_data = load_courses()
    
    if not any(courses_data.values()):
        send_telegram_message("📭 No courses monitored. Use /addcourse", chat_id)
        return
    
    message = "📚 <b>Monitored Courses</b>\n\n"
    for department, courses in courses_data.items():
        if courses:
            message += f"<b>{department}:</b>\n"
            for course in courses:
                message += f"• {course['code']}-{course['section']} (CRN: {course['crn']})\n"
            message += "\n"
    
    message += f"<i>Total: {sum(len(courses) for courses in courses_data.values())} courses</i>"
    send_telegram_message(message, chat_id)

def add_course(text, chat_id):
    """Add a course to monitoring"""
    try:
        parts = text.split()
        if len(parts) != 3:
            send_telegram_message("❌ Format: <code>/add COURSE-SECTION CRN</code>\nExample: <code>/add EE207-02 22716</code>", chat_id)
            return
        
        course_section = parts[1].upper()
        crn = parts[2]
        
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION (e.g., EE207-02)", chat_id)
            return
        
        course_code, section = course_section.split('-', 1)
        department = ''.join([c for c in course_code if not c.isdigit()])
        
        if not department:
            send_telegram_message("❌ Could not detect department", chat_id)
            return
        
        courses_data = load_courses()
        
        if department not in courses_data:
            courses_data[department] = []
        
        # Check if course already exists
        for course in courses_data[department]:
            if course['code'] == course_code and course['section'] == section:
                send_telegram_message(f"⚠️ Course {course_code}-{section} is already monitored!", chat_id)
                return
        
        new_course = {"code": course_code, "section": section, "crn": crn}
        courses_data[department].append(new_course)
        
        if save_courses(courses_data):
            send_telegram_message(f"✅ Added {course_code}-{section} (CRN: {crn}) to {department}!", chat_id)
        else:
            send_telegram_message("❌ Failed to save course", chat_id)
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", chat_id)

def remove_course(text, chat_id):
    """Remove a course from monitoring"""
    try:
        parts = text.split()
        if len(parts) != 2:
            send_telegram_message("❌ Format: <code>/remove COURSE-SECTION</code>\nExample: <code>/remove EE207-02</code>", chat_id)
            return
        
        course_section = parts[1].upper()
        
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION", chat_id)
            return
        
        course_code, section = course_section.split('-', 1)
        department = ''.join([c for c in course_code if not c.isdigit()])
        
        courses_data = load_courses()
        
        if department in courses_data:
            initial_count = len(courses_data[department])
            courses_data[department] = [
                course for course in courses_data[department]
                if not (course['code'] == course_code and course['section'] == section)
            ]
            
            if len(courses_data[department]) < initial_count:
                save_courses(courses_data)
                send_telegram_message(f"✅ Removed {course_code}-{section}!", chat_id)
            else:
                send_telegram_message(f"❌ Course {course_code}-{section} not found", chat_id)
        else:
            send_telegram_message(f"❌ No courses in {department}", chat_id)
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", chat_id)

def send_help(chat_id):
    message = """🆘 <b>Help</b>

<b>Commands:</b>
/start - Start bot
/status - Bot status  
/courses - Show courses
/addcourse - How to add
/remove [course] - Remove
/help - This message

<b>Check Interval:</b> 5 seconds ⚡"""
    send_telegram_message(message, chat_id)

def monitor_loop():
    """Main monitoring loop"""
    logger.info("🚀 Starting 5-second course monitor...")
    
    # Start Telegram commands
    commands_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
    commands_thread.start()
    
    # Send startup message
    courses_data = load_courses()
    courses_list = []
    for department, courses in courses_data.items():
        for course in courses:
            courses_list.append(f"• {course['code']}-{course['section']} (CRN: {course['crn']})")
    
    startup_message = f"""🤖 <b>Course Monitor Started!</b>

<b>Monitoring:</b>
{"\n".join(courses_list) if courses_list else "No courses - use /addcourse"}

<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Smart Rate Limiting:</b> ✅ Enabled
<b>Status:</b> 🟢 ACTIVE

Use /help for commands!"""

    send_telegram_message(startup_message)
    
    previous_available = set()
    check_count = 0
    
    while True:
        try:
            check_count += 1
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"🔍 Check #{check_count} at {current_time}")
            
            available_courses = check_course_availability()
            
            current_identifiers = set()
            for course in available_courses:
                identifier = f"{course['code']}-{course['section']}-{course['crn']}"
                current_identifiers.add(identifier)
            
            new_courses = current_identifiers - previous_available
            
            if new_courses:
                message = f"🎉 <b>COURSES AVAILABLE!</b> 🎉\n\n"
                for course in available_courses:
                    identifier = f"{course['code']}-{course['section']}-{course['crn']}"
                    if identifier in new_courses:
                        message += f"✅ <b>{course['code']}-{course['section']}</b>\n"
                        message += f"   📚 {course['title']}\n"
                        message += f"   👨‍🏫 {course['instructor']}\n"
                        message += f"   🕒 {course['schedule']}\n"
                        message += f"   🪑 Seats: <b>{course['seats']}</b>\n\n"
                
                message += f"🕒 {current_time}"
                send_telegram_message(message)
                logger.info(f"📤 Sent notification for {len(new_courses)} courses")
            
            previous_available = current_identifiers
            logger.info(f"✅ Check #{check_count} completed. Found {len(available_courses)} available")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"❌ Monitor error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    required_vars = ['BOT_TOKEN', 'CHAT_ID', 'WEBSITE_EMAIL', 'WEBSITE_PASSWORD']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    logger.info(f"🔧 Starting 5-second monitor with smart rate limiting")
    monitor_loop()
