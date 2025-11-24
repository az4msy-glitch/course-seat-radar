import requests
import time
import os
import logging
from datetime import datetime
import json
import threading
import random
from dataclasses import dataclass
from typing import Dict, List, Optional

# Set up logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('monitor.log')
    ]
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('BOT_TOKEN')
TELEGRAM_CHAT_IDS = os.getenv('CHAT_IDS', '').split(',')
CHECK_INTERVAL = 10
WEBSITE_EMAIL = os.getenv('WEBSITE_EMAIL')
WEBSITE_PASSWORD = os.getenv('WEBSITE_PASSWORD')

# API Endpoints
LOGIN_URL = "https://api.free-courses.dev/auth/login"
COURSES_URL = "https://api.free-courses.dev/courses"

# Courses storage
COURSES_FILE = "monitored_courses.json"

# ==================== THREAD-SAFE STATE MANAGEMENT ====================

@dataclass
class CourseState:
    status: str
    last_updated: float
    available_seats: int = 0

class ThreadSafeState:
    def __init__(self):
        self._lock = threading.RLock()
        self.course_status = "⏳ No data yet - first check in progress..."
        self.last_status_update = 0
        self.check_count = 0
        self.course_data: Dict[str, CourseState] = {}
    
    def update_status(self, status: str, course_data: List[dict] = None):
        with self._lock:
            self.course_status = status
            self.last_status_update = time.time()
            self.check_count += 1
            if course_data:
                for course in course_data:
                    key = f"{course['code']}-{course['section']}"
                    seats = course.get('seats', 'N/A')
                    available_seats = 0
                    
                    if seats and '/' in str(seats):
                        try:
                            current_seats, _ = str(seats).split('/')
                            available_seats = int(current_seats.strip())
                        except (ValueError, AttributeError):
                            available_seats = 0
                    
                    self.course_data[key] = CourseState(
                        status=seats,
                        last_updated=time.time(),
                        available_seats=available_seats
                    )
    
    def get_status(self) -> tuple:
        with self._lock:
            return self.course_status, self.last_status_update, self.check_count
    
    def get_course_data(self) -> Dict[str, CourseState]:
        with self._lock:
            return self.course_data.copy()

app_state = ThreadSafeState()

# ==================== CIRCUIT BREAKER PATTERN ====================

class CircuitBreaker:
    def __init__(self, failure_threshold=3, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._lock = threading.Lock()
    
    def can_execute(self):
        with self._lock:
            if self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    logger.info("🔓 Circuit breaker transitioning to HALF_OPEN")
                    return True
                logger.warning("🚧 Circuit breaker is OPEN, blocking execution")
                return False
            return True
    
    def record_success(self):
        with self._lock:
            self.failure_count = 0
            if self.state != "CLOSED":
                logger.info("✅ Circuit breaker reset to CLOSED")
            self.state = "CLOSED"
    
    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold and self.state != "OPEN":
                self.state = "OPEN"
                logger.error(f"🔒 Circuit breaker OPENED after {self.failure_count} failures")

api_circuit_breaker = CircuitBreaker()

# ==================== ENHANCED RATE LIMITING ====================

class AdvancedRateLimiter:
    def __init__(self):
        self.department_calls: Dict[str, float] = {}
        self.failure_counts: Dict[str, int] = {}
        self._lock = threading.Lock()
    
    def can_call_department(self, department: str) -> bool:
        with self._lock:
            now = time.time()
            last_call = self.department_calls.get(department, 0)
            
            # Adaptive cooldown based on failures
            failure_count = self.failure_counts.get(department, 0)
            cooldown = max(10, 10 * (2 ** min(failure_count, 3)))  # Max 80 seconds
            
            if (now - last_call) < cooldown:
                logger.debug(f"⏳ Rate limit active for {department}, {cooldown - (now - last_call):.1f}s remaining")
                return False
            return True
    
    def record_call(self, department: str, success: bool):
        with self._lock:
            self.department_calls[department] = time.time()
            if success:
                self.failure_counts[department] = 0
            else:
                self.failure_counts[department] = self.failure_counts.get(department, 0) + 1
                logger.warning(f"📉 {department} failure count: {self.failure_counts[department]}")

rate_limiter = AdvancedRateLimiter()

# ==================== SESSION MANAGEMENT ====================

class SessionManager:
    def __init__(self):
        self.session = None
        self.last_login = 0
        self.login_lock = threading.Lock()
        self.session_duration = 1500  # 25 minutes for safety
    
    def get_session(self):
        with self.login_lock:
            if (self.session is None or 
                time.time() - self.last_login > self.session_duration):
                return self._renew_session()
            return self.session
    
    def _renew_session(self):
        try:
            new_session = login_to_website()
            if new_session:
                self.session = new_session
                self.last_login = time.time()
                logger.info("🔄 Session renewed successfully")
            return new_session
        except Exception as e:
            logger.error(f"Session renewal failed: {e}")
            return None

session_manager = SessionManager()

# ==================== CORE FUNCTIONS ====================

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
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                success_count += 1
            else:
                logger.error(f"Failed to send to {chat_id}: {response.status_code}")
        
        return success_count > 0
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

def login_to_website():
    """Login to the course website"""
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
                logger.info("✅ Successfully logged in")
                return session
        else:
            logger.error(f"❌ Login failed: {response.status_code}")
        return None
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return None

def robust_api_call(session, url, params=None, max_retries=3):
    """Make API call with exponential backoff and circuit breaker"""
    if not api_circuit_breaker.can_execute():
        logger.warning("🚧 Circuit breaker is OPEN, skipping API call")
        return None
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                api_circuit_breaker.record_success()
                return response
            elif response.status_code == 429:
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"⏳ Rate limited, waiting {wait_time:.1f}s (attempt {attempt + 1})")
                time.sleep(wait_time)
            elif response.status_code == 401:
                logger.warning("🔑 Authentication expired")
                session_manager.session = None  # Force re-login
                return None
            else:
                logger.error(f"API error {response.status_code}, attempt {attempt + 1}")
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}, attempt {attempt + 1}")
        
        # Exponential backoff
        if attempt < max_retries - 1:
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    
    api_circuit_breaker.record_failure()
    return None

def get_department_courses(department):
    """Get courses for a specific department with enhanced error handling"""
    if not rate_limiter.can_call_department(department):
        logger.info(f"⏭️ Rate limit active for {department}")
        return []
    
    session = session_manager.get_session()
    if not session:
        logger.error("❌ No valid session available")
        rate_limiter.record_call(department, False)
        return []
    
    try:
        params = {"term": "252", "course": department}
        logger.info(f"📡 Fetching {department} courses...")
        
        response = robust_api_call(session, COURSES_URL, params)
        
        if response:
            try:
                courses_data = response.json()
                if isinstance(courses_data, list):
                    logger.info(f"✅ Got {len(courses_data)} courses for {department}")
                    rate_limiter.record_call(department, True)
                    return courses_data
                else:
                    logger.error(f"❌ Unexpected response format for {department}")
            except json.JSONDecodeError:
                logger.error(f"❌ Invalid JSON response for {department}")
        else:
            logger.error(f"❌ No response received for {department}")
        
        rate_limiter.record_call(department, False)
        return []
        
    except Exception as e:
        logger.error(f"Error getting {department} courses: {e}")
        rate_limiter.record_call(department, False)
        return []

def update_course_status(course_data):
    """Update course status in thread-safe manner"""
    try:
        if not course_data:
            app_state.update_status("📭 No courses found or API error")
            return
        
        status_lines = []
        for course in course_data:
            seats = course.get('seats', 'N/A')
            course_name = f"{course['code']}-{course['section']}"
            
            if seats and '/' in str(seats):
                try:
                    current_seats, total_seats = str(seats).split('/')
                    available_seats = int(current_seats.strip())
                    if available_seats > 0:
                        emoji = "🟢"
                    else:
                        emoji = "🔴"
                    status_lines.append(f"{emoji} {course_name}: {seats}")
                except (ValueError, AttributeError):
                    status_lines.append(f"⚫ {course_name}: {seats}")
            else:
                status_lines.append(f"⚫ {course_name}: {seats}")
        
        final_status = "\n".join(status_lines) if status_lines else "📭 No course data available"
        app_state.update_status(final_status, course_data)
        logger.info(f"📊 Updated course status with {len(status_lines)} courses")
        
    except Exception as e:
        logger.error(f"Error updating course status: {e}")
        app_state.update_status(f"❌ Error: {str(e)}")

def check_course_availability():
    """Check availability for all monitored courses"""
    try:
        all_available_courses = []
        all_course_data = []  # Track ALL courses for status
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
                        seats = found_course.get('seats', 'N/A')
                        course_name = f"{target_course['code']}-{target_course['section']}"
                        
                        # Store course data for status
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
        
        # UPDATE THE STATUS - Thread-safe
        update_course_status(all_course_data)
        
        # Log summary
        if all_course_data:
            logger.info(f"📊 Found {len(all_course_data)} courses total")
        else:
            logger.info("📊 No course data found")
        
        return all_available_courses
        
    except Exception as e:
        logger.error(f"Error checking availability: {e}")
        return []

# ==================== TELEGRAM COMMANDS ====================

def handle_telegram_commands():
    """Handle incoming Telegram commands"""
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            response = requests.get(url, params=params, timeout=35)
            
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
            elif response.status_code >= 500:
                logger.warning(f"Telegram API issue: {response.status_code}")
                time.sleep(10)
            
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
    else:
        send_telegram_message("❓ Use /help for commands", [chat_id])

def send_welcome_message(chat_id):
    message = """🤖 <b>Course Monitor Bot</b>

<b>Commands:</b>
/status - Bot status with seat availability
/seats - Quick seat status only  
/courses - Show monitored courses
/addcourse - How to add courses
/remove [course] - Remove course
/help - All commands

<b>Check Interval:</b> 10 seconds ⚡
<b>Seat Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown"""
    send_telegram_message(message, [chat_id])

def send_status(chat_id):
    """Send current course status with thread-safe data"""
    course_status, last_update, check_count = app_state.get_status()
    
    courses_data = load_courses()
    total_courses = sum(len(courses) for courses in courses_data.values())
    
    status_age = time.time() - last_update
    age_message = f"{int(status_age)} seconds ago" if status_age < 120 else "data is stale"
    
    # Circuit breaker status
    cb_status = "🟢 CLOSED" if api_circuit_breaker.state == "CLOSED" else "🔴 OPEN"
    
    message = f"""📊 <b>BOT STATUS</b>

<b>Monitoring:</b> {total_courses} courses
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Departments:</b> {', '.join(courses_data.keys())}
<b>Total Checks:</b> {check_count}
<b>Last Update:</b> {age_message}
<b>Circuit Breaker:</b> {cb_status}

<b>LIVE COURSE STATUS:</b>
{course_status}
"""
    
    send_telegram_message(message, [chat_id])

def send_seats_status(chat_id):
    """Quick seat status only"""
    course_status, last_update, _ = app_state.get_status()
    
    status_age = time.time() - last_update
    
    message = f"""🪑 <b>QUICK SEAT STATUS</b>

{course_status}

🕒 Updated {int(status_age)} seconds ago"""
    
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
/help - This message

<b>Check Interval:</b> 10 seconds ⚡
<b>Seat Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown"""
    send_telegram_message(message, [chat_id])

# ==================== RENDER.COM KEEP-ALIVE ====================

def keep_alive():
    """Prevent Render from sleeping the service"""
    while True:
        try:
            # Simple health check - log activity
            logger.info("❤️ Keep-alive heartbeat")
            time.sleep(300)  # Every 5 minutes
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")

# ==================== MAIN MONITORING LOOP ====================

def monitor_loop():
    """Main monitoring loop"""
    logger.info("🚀 Starting optimized 10-second course monitor...")
    
    # Start keep-alive thread for Render.com
    keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
    keep_alive_thread.start()
    
    # Start Telegram commands handler
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
<b>Status:</b> 🟢 ACTIVE
<b>Version:</b> Optimized with circuit breaker

Use /seats to see current seat status!"""

    send_telegram_message(startup_message)
    
    previous_available = set()
    
    while True:
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            course_status, last_update, check_count = app_state.get_status()
            logger.info(f"🔍 Check #{check_count + 1} at {current_time}")
            
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
            logger.info(f"✅ Check #{check_count + 1} completed. Found {len(available_courses)} available courses")
            time.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"❌ Monitor error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # Validate environment variables
    required_vars = ['BOT_TOKEN', 'CHAT_IDS', 'WEBSITE_EMAIL', 'WEBSITE_PASSWORD']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    # Validate Telegram chat IDs
    if not TELEGRAM_CHAT_IDS or not any(TELEGRAM_CHAT_IDS):
        logger.error("❌ No valid Telegram chat IDs configured")
        exit(1)
    
    logger.info("🔧 Starting optimized monitor with circuit breaker")
    monitor_loop()
