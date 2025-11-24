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
TELEGRAM_CHAT_IDS = os.getenv('CHAT_IDS').split(',')
CHECK_INTERVAL = 10
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
SESSION_DURATION = 1800
MIN_API_INTERVAL = 3
last_department_call = {}

# Enhanced course tracking
last_course_data = {}  # Store the latest course data for status commands
last_status_check = 0
check_count = 0

# Circuit breaker protection
department_failures = {}
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 300  # 5 minutes

def load_courses():
    """Load monitored courses from file"""
    try:
        if os.path.exists(COURSES_FILE):
            with open(COURSES_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading courses: {e}")
    
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

def send_telegram_message(message, chat_ids=None):
    """Send message to Telegram - supports multiple chat IDs"""
    try:
        if chat_ids is None:
            chat_ids = TELEGRAM_CHAT_IDS
            
        success_count = 0
        for chat_id in chat_ids:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            response = requests.post(url, data=data)
            if response.status_code == 200:
                success_count += 1
            else:
                logger.error(f"Failed to send to {chat_id}: {response.status_code}")
        
        return success_count > 0
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def smart_rate_limit(department):
    """Smart rate limiting per department"""
    global last_department_call
    current_time = time.time()
    
    if department in last_department_call:
        time_since_last_call = current_time - last_department_call[department]
        if time_since_last_call < 10:
            return
    
    last_department_call[department] = current_time

def is_department_circuit_open(department):
    """Check if circuit breaker is open for a department"""
    if department in department_failures:
        failures, last_failure = department_failures[department]
        if failures >= CIRCUIT_BREAKER_THRESHOLD:
            if time.time() - last_failure < CIRCUIT_BREAKER_TIMEOUT:
                logger.warning(f"🔌 Circuit breaker OPEN for {department}")
                return True
            else:
                # Reset after timeout
                del department_failures[department]
                logger.info(f"🔌 Circuit breaker RESET for {department}")
    return False

def record_department_failure(department):
    """Record a failure for circuit breaker"""
    if department not in department_failures:
        department_failures[department] = [1, time.time()]
    else:
        department_failures[department][0] += 1
        department_failures[department][1] = time.time()

def record_department_success(department):
    """Record success to reset circuit breaker"""
    if department in department_failures:
        del department_failures[department]

def cleanup_old_rate_limits():
    """Clean up old rate limit entries to prevent memory leaks"""
    global last_department_call
    current_time = time.time()
    old_keys = []
    
    for department, last_call in last_department_call.items():
        if current_time - last_call > 3600:  # 1 hour
            old_keys.append(department)
    
    for key in old_keys:
        del last_department_call[key]
    
    if old_keys:
        logger.info(f"🧹 Cleaned up {len(old_keys)} old rate limit entries")

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
        response = session.post(LOGIN_URL, json=login_data, timeout=10)
        
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

def find_course_match(target_course, department_courses):
    """Enhanced course matching with multiple fallback strategies"""
    if not isinstance(department_courses, list):
        return None
    
    target_code = target_course['code']
    target_section = target_course['section']
    target_crn = target_course['crn']
    
    for course in department_courses:
        if not isinstance(course, dict):
            continue
            
        # Strategy 1: Exact code + section match
        course_code = course.get('code', '').strip()
        course_section = course.get('section', '').strip()
        
        if (course_code == target_code and course_section == target_section):
            return course
        
        # Strategy 2: CRN match
        course_crn = str(course.get('crn', '')).strip()
        if course_crn == target_crn:
            return course
        
        # Strategy 3: Case-insensitive code match
        if (course_code.upper() == target_code.upper() and 
            course_section.upper() == target_section.upper()):
            return course
    
    return None

def get_department_courses(department):
    """Get courses for a specific department with enhanced rate limiting"""
    # Check circuit breaker first
    if is_department_circuit_open(department):
        return []
    
    session = get_session()
    if not session:
        return []
    
    try:
        current_time = time.time()
        if department in last_department_call:
            time_since_last_call = current_time - last_department_call[department]
            if time_since_last_call < 10:
                logger.info(f"⏭️ Skipping {department} - rate limit cooldown")
                return []
        
        smart_rate_limit(department)
        
        params = {"term": "252", "course": department}
        
        logger.info(f"📡 Fetching {department} courses...")
        response = session.get(COURSES_URL, params=params, timeout=10)
        
        if response.status_code == 200:
            try:
                courses_data = response.json()
                if isinstance(courses_data, list):
                    logger.info(f"✅ Got {len(courses_data)} courses for {department}")
                    record_department_success(department)
                return courses_data
            except json.JSONDecodeError:
                logger.error(f"❌ Invalid JSON response for {department}")
                record_department_failure(department)
                return []
        elif response.status_code == 429:
            logger.warning(f"⚠️ Rate limited for {department}. Cooldown: 30 seconds")
            last_department_call[department] = time.time() + 20  # Extended cooldown
            record_department_failure(department)
            return []
        elif response.status_code == 401:
            logger.warning("🔄 Token expired, forcing relogin...")
            global current_session
            current_session = None
            return []
        elif response.status_code >= 500:
            logger.error(f"❌ Server error for {department}: {response.status_code}")
            record_department_failure(department)
            return []
        else:
            logger.error(f"❌ Failed to get {department} courses: {response.status_code}")
            record_department_failure(department)
            return []
            
    except requests.exceptions.Timeout:
        logger.error(f"⏰ Timeout fetching {department} courses")
        record_department_failure(department)
        return []
    except requests.exceptions.ConnectionError:
        logger.error(f"🔌 Connection error fetching {department} courses")
        record_department_failure(department)
        return []
    except Exception as e:
        logger.error(f"❌ Unexpected error getting {department} courses: {e}")
        record_department_failure(department)
        return []

def format_course_status(course_data):
    """Format course data for Telegram status display"""
    if not course_data:
        return ["No course data available"]
    
    status_lines = []
    for course in course_data:
        seats = course.get('seats', 'N/A')
        course_name = f"{course['code']}-{course['section']}"
        
        if seats and '/' in str(seats):
            try:
                current_seats, total_seats = str(seats).split('/')
                available_seats = int(current_seats.strip())
                if available_seats > 0:
                    emoji = "🟢"  # Seats available
                else:
                    emoji = "🔴"  # Full
                status_lines.append(f"{emoji} {course_name}: {seats}")
            except (ValueError, AttributeError):
                status_lines.append(f"⚫ {course_name}: {seats}")
        else:
            status_lines.append(f"⚫ {course_name}: {seats}")
    
    return status_lines

def get_latest_course_status():
    """Get the latest course status from recent data"""
    global last_course_data, last_status_check
    
    try:
        if not last_course_data:
            return ["⏳ No course data available yet\nNext update in 10 seconds..."]
        
        status_age = time.time() - last_status_check
        if status_age > 120:  # 2 minutes
            return ["⏳ Course data is stale\nNext update in 10 seconds..."]
        
        return format_course_status(last_course_data)
    except Exception as e:
        logger.error(f"Error getting course status: {e}")
        return ["❌ Error loading course status"]

def check_course_availability():
    """Check availability for all monitored courses - ENHANCED with status tracking"""
    global last_course_data, last_status_check
    
    try:
        all_available_courses = []
        all_course_data = []  # Track ALL courses for status display
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
                    found_course = find_course_match(target_course, department_courses)
                    
                    if found_course:
                        seats = found_course.get('seats', 'N/A')
                        course_name = f"{target_course['code']}-{target_course['section']}"
                        
                        # Store course data for status display
                        course_info = {
                            'code': target_course['code'],
                            'section': target_course['section'],
                            'seats': seats,
                            'department': department
                        }
                        all_course_data.append(course_info)
                        
                        if seats and '/' in str(seats):
                            try:
                                current_seats, total_seats = str(seats).split('/')
                                available_seats = int(current_seats.strip())
                                if available_seats > 0:
                                    detailed_info = {
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
                                    all_available_courses.append(detailed_info)
                                    logger.info(f"🎯 AVAILABLE: {department} {course_name} - {seats}")
                                else:
                                    logger.info(f"📊 {department} {course_name} - {seats}")
                            except (ValueError, AttributeError) as e:
                                logger.error(f"Error parsing seats for {course_name}: {e}")
                        else:
                            logger.info(f"📊 {department} {course_name} - {seats}")
                    else:
                        logger.warning(f"❓ Course not found: {target_course['code']}-{target_course['section']}")
        
        # Update global course data for status commands
        last_course_data = all_course_data
        last_status_check = time.time()
        
        # Log summary
        if all_course_data:
            status_summary = format_course_status(all_course_data)
            logger.info(f"📊 COURSE STATUS: {', '.join([s.split(': ')[1] for s in status_summary])}")
        else:
            logger.info("📊 No course data found")
        
        return all_available_courses
        
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return []

# Telegram Commands
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
                        
                        if str(chat_id) in TELEGRAM_CHAT_IDS and text:
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
    elif text_lower == "/seats":
        send_seats_status(chat_id)
    elif text_lower == "/courses":
        send_monitored_courses(chat_id)
    elif text_lower == "/addcourse":
        send_telegram_message("📝 To add a course:\n<code>/add COURSE-SECTION CRN</code>\nExample: <code>/add EE207-02 22716</code>", [chat_id])
    elif text_lower.startswith("/add "):
        add_course(text, chat_id)
    elif text_lower.startswith("/remove "):
        remove_course(text, chat_id)
    elif text_lower == "/help":
        send_help(chat_id)
    elif text_lower == "/force_update":
        force_status_update(chat_id)
    else:
        send_telegram_message("❓ Use /help for commands", [chat_id])

def send_welcome_message(chat_id):
    message = """🤖 <b>ULTIMATE Course Monitor Bot</b>

<b>Commands:</b>
/status - Bot status with seat availability
/seats - Quick seat status only  
/courses - Show monitored courses
/addcourse - How to add courses
/remove [course] - Remove course
/force_update - Force immediate status update
/help - All commands

<b>Check Interval:</b> 10 seconds ⚡
<b>Seat Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown

<b>Features:</b>
✅ Smart rate limiting
✅ Circuit breaker protection  
✅ Enhanced error handling
✅ Real-time seat tracking"""
    send_telegram_message(message, [chat_id])

def send_status(chat_id):
    """Send current course status with seat availability"""
    course_status = get_latest_course_status()
    courses_data = load_courses()
    total_courses = sum(len(courses) for courses in courses_data.values())
    
    status_age = time.time() - last_status_check
    age_message = f"{int(status_age)} seconds ago" if status_age < 60 else "over a minute ago"
    
    # Department health
    department_health = []
    for department in courses_data.keys():
        last_call = last_department_call.get(department, 0)
        health = "🟢" if time.time() - last_call < 60 else "🟡"
        department_health.append(f"{health} {department}")
    
    message = f"""📊 <b>ENHANCED BOT STATUS</b>

<b>Monitoring:</b> {total_courses} courses
<b>Departments:</b> {len(courses_data)}
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Total Checks:</b> {check_count}
<b>Last Update:</b> {age_message}

<b>DEPARTMENT HEALTH:</b>
{', '.join(department_health)}

<b>LIVE COURSE STATUS:</b>
"""
    
    message += "\n" + "\n".join(course_status)
    message += f"\n\n🕒 Next update in {max(0, CHECK_INTERVAL - (status_age % CHECK_INTERVAL)):.0f} seconds"
    
    send_telegram_message(message, [chat_id])

def send_seats_status(chat_id):
    """Quick seat status only"""
    course_status = get_latest_course_status()
    
    status_age = time.time() - last_status_check
    
    message = "🪑 <b>QUICK SEAT STATUS</b>\n\n"
    message += "\n".join(course_status)
    message += f"\n\n🕒 Updated {int(status_age)} seconds ago"
    
    send_telegram_message(message, [chat_id])

def send_monitored_courses(chat_id):
    courses_data = load_courses()
    
    if not any(courses_data.values()):
        send_telegram_message("📭 No courses monitored. Use /addcourse", [chat_id])
        return
    
    message = "📚 <b>Monitored Courses</b>\n\n"
    for department, courses in courses_data.items():
        if courses:
            message += f"<b>{department}:</b>\n"
            for course in courses:
                message += f"• {course['code']}-{course['section']} (CRN: {course['crn']})\n"
            message += "\n"
    
    message += f"<i>Total: {sum(len(courses) for courses in courses_data.values())} courses</i>"
    send_telegram_message(message, [chat_id])

def force_status_update(chat_id):
    """Force an immediate status update"""
    global last_status_check
    last_status_check = 0  # Force refresh on next status check
    send_telegram_message("🔄 Forcing course status update...\nUse /seats in 10 seconds to see fresh data.", [chat_id])

def add_course(text, chat_id):
    """Add a course to monitoring"""
    try:
        parts = text.split()
        if len(parts) != 3:
            send_telegram_message("❌ Format: <code>/add COURSE-SECTION CRN</code>\nExample: <code>/add EE207-02 22716</code>", [chat_id])
            return
        
        course_section = parts[1].upper()
        crn = parts[2]
        
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION (e.g., EE207-02)", [chat_id])
            return
        
        course_code, section = course_section.split('-', 1)
        department = ''.join([c for c in course_code if not c.isdigit()])
        
        if not department:
            send_telegram_message("❌ Could not detect department", [chat_id])
            return
        
        courses_data = load_courses()
        
        if department not in courses_data:
            courses_data[department] = []
        
        # Check if course already exists
        for course in courses_data[department]:
            if course['code'] == course_code and course['section'] == section:
                send_telegram_message(f"⚠️ Course {course_code}-{section} is already monitored!", [chat_id])
                return
        
        new_course = {"code": course_code, "section": section, "crn": crn}
        courses_data[department].append(new_course)
        
        if save_courses(courses_data):
            success_message = f"✅ Added {course_code}-{section} (CRN: {crn}) to {department}!"
            send_telegram_message(success_message)
        else:
            send_telegram_message("❌ Failed to save course", [chat_id])
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", [chat_id])

def remove_course(text, chat_id):
    """Remove a course from monitoring"""
    try:
        parts = text.split()
        if len(parts) != 2:
            send_telegram_message("❌ Format: <code>/remove COURSE-SECTION</code>\nExample: <code>/remove EE207-02</code>", [chat_id])
            return
        
        course_section = parts[1].upper()
        
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION", [chat_id])
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
                removal_message = f"✅ Removed {course_code}-{section}!"
                send_telegram_message(removal_message)
            else:
                send_telegram_message(f"❌ Course {course_code}-{section} not found", [chat_id])
        else:
            send_telegram_message(f"❌ No courses in {department}", [chat_id])
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", [chat_id])

def send_help(chat_id):
    message = """🆘 <b>Help</b>

<b>Commands:</b>
/start - Start bot
/status - Bot status with seat availability
/seats - Quick seat status only
/courses - Show monitored courses
/addcourse - How to add courses
/remove [course] - Remove course
/force_update - Force immediate status update
/help - This message

<b>Check Interval:</b> 10 seconds ⚡
<b>Seat Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown

<b>Note:</b> Status updates automatically every 10 seconds"""
    send_telegram_message(message, [chat_id])

def monitor_loop():
    """Enhanced main monitoring loop with health checks"""
    global check_count
    
    logger.info("🚀 Starting ULTIMATE 10-second course monitor...")
    
    # Start Telegram commands
    commands_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
    commands_thread.start()
    
    # Send startup message
    courses_data = load_courses()
    courses_list = []
    for department, courses in courses_data.items():
        for course in courses:
            courses_list.append(f"• {course['code']}-{course['section']} (CRN: {course['crn']})")
    
    startup_message = f"""🤖 <b>ULTIMATE Course Monitor Started!</b>

<b>Monitoring:</b> {sum(len(courses) for courses in courses_data.values())} courses
<b>Departments:</b> {', '.join(courses_data.keys())}
<b>Features:</b> 
✅ Real-time seat status
✅ Smart rate limiting  
✅ Circuit breaker protection
✅ Enhanced error handling
✅ 10-second speed ⚡

Use /status for detailed metrics!"""

    send_telegram_message(startup_message)
    
    previous_available = set()
    check_count = 0
    
    while True:
        try:
            check_count += 1
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Cleanup every 10 minutes
            if check_count % 60 == 0:
                cleanup_old_rate_limits()
            
            logger.info(f"🔍 Check #{check_count} at {current_time}")
            
            # This now automatically updates course status data
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
                
                message += f"🕒 {current_time}\n"
                message += f"🔔 Check #{check_count}"
                send_telegram_message(message)
                logger.info(f"📤 Sent notification for {len(new_courses)} courses")
            
            previous_available = current_identifiers
            
            # Health check logging
            if check_count % 30 == 0:  # Every 5 minutes
                logger.info(f"📈 Health Check - Total checks: {check_count}, Active departments: {len(last_department_call)}")
            
            logger.info(f"✅ Check #{check_count} completed. Found {len(available_courses)} available courses")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"❌ Monitor loop error: {e}")
            time.sleep(min(60, CHECK_INTERVAL * 2))  # Backoff on repeated errors

if __name__ == "__main__":
    required_vars = ['BOT_TOKEN', 'CHAT_IDS', 'WEBSITE_EMAIL', 'WEBSITE_PASSWORD']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    logger.info("🔧 Starting ULTIMATE 10-second monitor with enhanced reliability")
    monitor_loop()
