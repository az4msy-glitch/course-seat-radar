import requests
import time
import os
import logging
from datetime import datetime
import json
import threading

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)  # ← FIXED THIS LINE

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('CHAT_ID')
CHECK_INTERVAL = 2  # 2 seconds ⚡
WEBSITE_EMAIL = os.getenv('WEBSITE_EMAIL')
WEBSITE_PASSWORD = os.getenv('WEBSITE_PASSWORD')

# API Endpoints
LOGIN_URL = "https://api.free-courses.dev/auth/login"
COURSES_URL = "https://api.free-courses.dev/courses"

# Courses storage (instead of hardcoded)
COURSES_FILE = "monitored_courses.json"

# Global session to reuse
current_session = None
last_login_time = 0
SESSION_DURATION = 1800  # 30 minutes before relogin

def load_courses():
    """Load monitored courses from file"""
    try:
        if os.path.exists(COURSES_FILE):
            with open(COURSES_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading courses: {e}")
    
    # Default courses if file doesn't exist
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
        courses_data = load_courses()
        
        # Check each department
        for department, courses in courses_data.items():
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
        send_telegram_message("📝 To add a course, use this format:\n\n<code>/add EE207-02 22716</code>\n\nWhere:\n• EE207-02 = Course Code-Section\n• 22716 = CRN", chat_id)
    elif text_lower.startswith("/add "):
        add_course(text, chat_id)
    elif text_lower.startswith("/remove "):
        remove_course(text, chat_id)
    elif text_lower == "/help":
        send_help(chat_id)
    else:
        send_telegram_message("❓ Unknown command. Use /help for available commands.", chat_id)

def send_welcome_message(chat_id):
    """Send welcome message with commands"""
    message = """🤖 <b>Course Monitor Bot</b>

<b>Available Commands:</b>
/status - Check bot status
/courses - Show monitored courses
/addcourse - How to add a course
/remove [course] - Remove a course
/help - Show all commands

<b>Example:</b>
<code>/add EE207-02 22716</code> - Add EE207 section 02
<code>/remove EE207-02</code> - Remove EE207 section 02"""
    
    send_telegram_message(message, chat_id)

def send_status(chat_id):
    """Send current bot status"""
    courses_data = load_courses()
    total_courses = sum(len(courses) for courses in courses_data.values())
    
    message = f"""📊 <b>Bot Status</b>

<b>Monitoring:</b> {total_courses} courses
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Departments:</b> {', '.join(courses_data.keys())}
<b>Status:</b> 🟢 ACTIVE
<b>Last Check:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
    
    send_telegram_message(message, chat_id)

def send_monitored_courses(chat_id):
    """Send list of monitored courses"""
    courses_data = load_courses()
    
    if not any(courses_data.values()):
        send_telegram_message("📭 No courses being monitored. Use /addcourse to add courses.", chat_id)
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
        
        # Parse course code and section
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION (e.g., EE207-02)", chat_id)
            return
        
        course_code, section = course_section.split('-', 1)
        department = course_code[:3]  # Extract department from course code
        
        # Load current courses
        courses_data = load_courses()
        
        # Initialize department if not exists
        if department not in courses_data:
            courses_data[department] = []
        
        # Check if course already exists
        for course in courses_data[department]:
            if course['code'] == course_code and course['section'] == section:
                send_telegram_message(f"⚠️ Course {course_code}-{section} is already being monitored!", chat_id)
                return
        
        # Add new course
        new_course = {
            "code": course_code,
            "section": section,
            "crn": crn
        }
        courses_data[department].append(new_course)
        
        # Save courses
        if save_courses(courses_data):
            send_telegram_message(f"✅ Added {course_code}-{section} (CRN: {crn}) to monitoring!", chat_id)
        else:
            send_telegram_message("❌ Failed to save course. Check logs.", chat_id)
            
    except Exception as e:
        send_telegram_message(f"❌ Error adding course: {str(e)}", chat_id)

def remove_course(text, chat_id):
    """Remove a course from monitoring"""
    try:
        parts = text.split()
        if len(parts) != 2:
            send_telegram_message("❌ Format: <code>/remove COURSE-SECTION</code>\nExample: <code>/remove EE207-02</code>", chat_id)
            return
        
        course_section = parts[1].upper()
        
        # Parse course code and section
        if '-' not in course_section:
            send_telegram_message("❌ Use format: COURSE-SECTION (e.g., EE207-02)", chat_id)
            return
        
        course_code, section = course_section.split('-', 1)
        department = course_code[:3]
        
        # Load current courses
        courses_data = load_courses()
        
        # Remove course
        if department in courses_data:
            initial_count = len(courses_data[department])
            courses_data[department] = [
                course for course in courses_data[department]
                if not (course['code'] == course_code and course['section'] == section)
            ]
            
            if len(courses_data[department]) < initial_count:
                save_courses(courses_data)
                send_telegram_message(f"✅ Removed {course_code}-{section} from monitoring!", chat_id)
            else:
                send_telegram_message(f"❌ Course {course_code}-{section} not found in monitoring.", chat_id)
        else:
            send_telegram_message(f"❌ No courses found for department {department}.", chat_id)
            
    except Exception as e:
        send_telegram_message(f"❌ Error removing course: {str(e)}", chat_id)

def send_help(chat_id):
    """Send help message"""
    message = """🆘 <b>Course Monitor Bot - Help</b>

<b>Commands:</b>
/start - Start the bot
/status - Check bot status  
/courses - Show all monitored courses
/addcourse - Show how to add courses
/remove [course] - Remove a course
/help - This message

<b>Adding Courses:</b>
<code>/add COURSE-SECTION CRN</code>
Example: <code>/add EE207-02 22716</code>

<b>Removing Courses:</b>
<code>/remove COURSE-SECTION</code>
Example: <code>/remove EE207-02</code>

<b>Current Check Interval:</b> Every 2 seconds ⚡"""
    
    send_telegram_message(message, chat_id)

def monitor_loop():
    """Main monitoring loop"""
    logger.info("🚀 Starting course availability monitor...")
    
    # Start Telegram commands handler in background
    commands_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
    commands_thread.start()
    
    # Send startup message
    courses_data = load_courses()
    courses_list = []
    for department, courses in courses_data.items():
        for course in courses:
            courses_list.append(f"• {course['code']}-{course['section']} (CRN: {course['crn']})")
    
    startup_message = f"""🤖 <b>Course Monitor Started!</b>

<b>Monitoring Courses:</b>
{"\n".join(courses_list) if courses_list else "No courses yet - use /addcourse"}

<b>Term:</b> 252
<b>Check Interval:</b> Every {CHECK_INTERVAL} seconds ⚡
<b>Interactive:</b> ✅ Enabled
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
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"❌ Monitor error: {e}")
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
