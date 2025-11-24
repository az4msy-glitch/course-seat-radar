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

# Enhanced course structure for section monitoring
DEFAULT_COURSES = {
    "EE": [
        {
            "code": "EE207", 
            "sections": [
                {"section": "02", "crn": "22716"},
                {"section": "03", "crn": "22717"}
            ]
        },
        {
            "code": "EE271",
            "sections": [
                {"section": "53", "crn": "20825"},
                {"section": "54", "crn": "20826"}
            ]
        }
    ],
    "ENGL": [
        {
            "code": "ENGL214",
            "sections": [
                {"section": "14", "crn": "21510"},
                {"section": "15", "crn": "21511"}
            ]
        }
    ]
}

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
                    
                    if self.has_available_seats(seats):
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
    
    def has_available_seats(self, seats):
        """Check if seats string indicates availability"""
        if not seats or seats == 'N/A':
            return False
        
        try:
            if '/' in str(seats):
                current, total = str(seats).split('/')
                return int(current.strip()) > 0
            else:
                # Try to parse as number directly
                return int(str(seats).strip()) > 0
        except (ValueError, AttributeError):
            return False

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
    
    return DEFAULT_COURSES

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
    """Get courses for a specific department with flexible response handling"""
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
        
        if response and response.status_code == 200:
            data = response.json()
            
            # EXTRACT COURSES FROM THE 'data' KEY
            courses_list = []
            if isinstance(data, dict) and 'data' in data and isinstance(data['data'], list):
                courses_list = data['data']
                logger.info(f"✅ Got {len(courses_list)} courses for {department} from 'data' key")
            elif isinstance(data, list):
                courses_list = data
                logger.info(f"✅ Got {len(courses_list)} courses for {department} (direct list)")
            else:
                logger.error(f"❌ Unexpected response format for {department}")
                logger.error(f"Response type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                rate_limiter.record_call(department, False)
                return []
            
            rate_limiter.record_call(department, True)
            return courses_list
        else:
            logger.error(f"❌ No valid response for {department} (status: {response.status_code if response else 'No response'})")
            rate_limiter.record_call(department, False)
            return []
        
    except Exception as e:
        logger.error(f"Error getting {department} courses: {e}")
        rate_limiter.record_call(department, False)
        return []

def has_available_seats(seats):
    """Check if seats string indicates availability"""
    if not seats or seats == 'N/A':
        return False
    
    try:
        if '/' in str(seats):
            current, total = str(seats).split('/')
            return int(current.strip()) > 0
        else:
            # Try to parse as number directly
            return int(str(seats).strip()) > 0
    except (ValueError, AttributeError):
        return False

def find_section_data(course_data, target_section):
    """Find specific section data within course information - FIXED VERSION"""
    try:
        # The API returns individual sections as separate course entries
        # Each course in the 'data' array represents one section
        
        # Check if this course entry matches our target section
        course_section = course_data.get('section', '')
        course_crn = course_data.get('crn', '')
        
        # Match by section number or CRN
        if (course_section == target_section['section'] or 
            str(course_crn) == str(target_section['crn'])):
            return course_data
        
        # Also check if sections are nested (alternative structure)
        if 'sections' in course_data and isinstance(course_data['sections'], list):
            for section in course_data['sections']:
                if (section.get('section') == target_section['section'] or 
                    str(section.get('crn')) == str(target_section['crn'])):
                    return section
        
        return None
        
    except Exception as e:
        logger.error(f"Error finding section data: {e}")
        return None

def check_section_availability():
    """Check availability for specific sections with detailed tracking - FIXED VERSION"""
    try:
        available_sections = []
        all_section_data = []
        courses_data = load_courses()
        
        logger.info(f"🔍 Checking {sum(len(course['sections']) for dept in courses_data.values() for course in dept)} sections across {len(courses_data)} departments")
        
        for department, courses in courses_data.items():
            if not courses:
                continue
                
            department_courses = get_department_courses(department)
            if not department_courses:
                logger.warning(f"❌ No courses returned for {department}")
                continue
            
            logger.info(f"📊 Processing {len(department_courses)} courses from {department}")
            
            # Check each course and its sections
            for target_course in courses:
                course_code = target_course['code']
                logger.info(f"  Looking for {course_code} in {len(department_courses)} courses")
                
                # Find ALL courses that match our target course code
                matching_courses = []
                for course in department_courses:
                    if isinstance(course, dict) and course.get('code') == course_code:
                        matching_courses.append(course)
                
                logger.info(f"  Found {len(matching_courses)} instances of {course_code}")
                
                # Check each section we're monitoring
                for target_section in target_course['sections']:
                    section_found = False
                    
                    for course_instance in matching_courses:
                        section_data = find_section_data(course_instance, target_section)
                        
                        if section_data:
                            section_found = True
                            seats = section_data.get('seats', 'N/A')
                            section_info = {
                                'department': department,
                                'code': course_code,
                                'section': target_section['section'],
                                'crn': target_section['crn'],
                                'seats': seats,
                                'title': section_data.get('title', course_instance.get('title', 'N/A')),
                                'instructor': section_data.get('instructor', course_instance.get('instructor', 'N/A')),
                                'schedule': f"{section_data.get('days', 'N/A')} {section_data.get('time', 'N/A')}",
                                'location': section_data.get('location', 'N/A')
                            }
                            
                            all_section_data.append(section_info)
                            
                            # Check if section has seats
                            if has_available_seats(seats):
                                available_sections.append(section_info)
                                logger.info(f"🎯 SECTION AVAILABLE: {department} {course_code}-{target_section['section']} - {seats}")
                            else:
                                logger.info(f"📊 Section status: {department} {course_code}-{target_section['section']} - {seats}")
                            break
                    
                    if not section_found:
                        logger.warning(f"❌ Could not find data for {department} {course_code}-{target_section['section']}")
        
        # Update global status with section data
        update_section_status(all_section_data)
        
        logger.info(f"📊 Section check: {len(available_sections)} available out of {len(all_section_data)} monitored sections")
        return available_sections
        
    except Exception as e:
        logger.error(f"Error checking section availability: {e}")
        return []

def update_section_status(section_data):
    """Update global status with section-level information"""
    try:
        if not section_data:
            app_state.update_status("📭 No section data available")
            return
        
        status_lines = []
        available_count = 0
        
        for section in section_data:
            seats = section.get('seats', 'N/A')
            display_name = f"{section['code']}-{section['section']}"
            
            if has_available_seats(seats):
                emoji = "🟢"
                available_count += 1
                status_lines.append(f"{emoji} {display_name}: {seats} ✅")
            else:
                emoji = "🔴"
                status_lines.append(f"{emoji} {display_name}: {seats}")
        
        # Add summary line
        if available_count > 0:
            summary = f"\n🎉 {available_count} SECTIONS AVAILABLE!"
            status_lines.append(summary)
        
        final_status = "\n".join(status_lines) if status_lines else "📭 No section data available"
        app_state.update_status(final_status, section_data)
        logger.info(f"📊 Updated section status: {available_count} available sections")
        
    except Exception as e:
        logger.error(f"Error updating section status: {e}")
        app_state.update_status(f"❌ Error: {str(e)}")

def send_section_notification(available_sections, chat_ids=None):
    """Send detailed section availability notifications"""
    if not available_sections:
        return
    
    message = "🎉 <b>SECTION AVAILABLE!</b> 🎉\n\n"
    
    for section in available_sections:
        message += f"✅ <b>{section['code']}-{section['section']}</b> (CRN: {section['crn']})\n"
        message += f"   📚 {section['title']}\n"
        message += f"   👨‍🏫 {section['instructor']}\n"
        message += f"   🕒 {section['schedule']}\n"
        message += f"   📍 {section['location']}\n"
        message += f"   🪑 Seats: <b>{section['seats']}</b>\n\n"
    
    message += f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"⚡ Detected in {CHECK_INTERVAL} seconds"
    
    send_telegram_message(message, chat_ids)
    logger.info(f"📤 Sent notification for {len(available_sections)} available sections")

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
        send_section_status(chat_id)
    elif text_lower == "/seats":
        send_seats_status(chat_id)
    elif text_lower == "/courses":
        send_monitored_courses(chat_id)
    elif text_lower == "/addcourse":
        send_telegram_message("📝 To add a course section:\n<code>/add COURSE-SECTION CRN</code>\nExample: <code>/add EE207-02 22716</code>", [chat_id])
    elif text_lower.startswith("/add "):
        add_course_section(text, chat_id)
    elif text_lower.startswith("/remove "):
        remove_course_section(text, chat_id)
    elif text_lower == "/help":
        send_help(chat_id)
    else:
        send_telegram_message("❓ Use /help for commands", [chat_id])

def send_welcome_message(chat_id):
    message = """🤖 <b>Section Monitor Bot</b>

<b>Commands:</b>
/status - Bot status with section availability
/seats - Quick section status only  
/courses - Show monitored sections
/addcourse - How to add sections
/remove [section] - Remove section
/help - All commands

<b>Check Interval:</b> 10 seconds ⚡
<b>Section Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown"""
    send_telegram_message(message, [chat_id])

def send_section_status(chat_id):
    """Send detailed section status"""
    course_status, last_update, check_count = app_state.get_status()
    
    courses_data = load_courses()
    total_sections = sum(
        len(course['sections']) 
        for department in courses_data.values() 
        for course in department
    )
    
    status_age = time.time() - last_update
    
    # Circuit breaker status
    cb_status = "🟢 CLOSED" if api_circuit_breaker.state == "CLOSED" else "🔴 OPEN"
    
    message = f"""📊 <b>SECTION MONITOR STATUS</b>

<b>Monitoring:</b> {total_sections} sections
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Total Checks:</b> {check_count}
<b>Last Update:</b> {int(status_age)} seconds ago
<b>Circuit Breaker:</b> {cb_status}

<b>SECTION AVAILABILITY:</b>
{course_status}"""

    send_telegram_message(message, [chat_id])

def send_seats_status(chat_id):
    """Quick section status only"""
    course_status, last_update, _ = app_state.get_status()
    
    status_age = time.time() - last_update
    
    message = f"""🪑 <b>QUICK SECTION STATUS</b>

{course_status}

🕒 Updated {int(status_age)} seconds ago"""
    
    send_telegram_message(message, [chat_id])

def send_monitored_courses(chat_id):
    courses_data = load_courses()
    
    if not any(courses_data.values()):
        send_telegram_message("📭 No sections monitored. Use /addcourse", [chat_id])
        return
    
    total_sections = sum(
        len(course['sections']) 
        for department in courses_data.values() 
        for course in department
    )
    
    message = "📚 <b>Monitored Sections</b>\n\n"
    for department, courses in courses_data.items():
        if courses:
            message += f"<b>{department}:</b>\n"
            for course in courses:
                message += f"  {course['code']}:\n"
                for section in course['sections']:
                    message += f"    • Section {section['section']} (CRN: {section['crn']})\n"
            message += "\n"
    
    message += f"<i>Total: {total_sections} sections</i>"
    send_telegram_message(message, [chat_id])

def add_course_section(text, chat_id):
    """Add a course section to monitoring"""
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
        
        # Find existing course or create new
        existing_course = None
        for course in courses_data[department]:
            if course['code'] == course_code:
                existing_course = course
                break
        
        if existing_course:
            # Check if section already exists
            for existing_section in existing_course['sections']:
                if existing_section['section'] == section:
                    send_telegram_message(f"⚠️ Section {course_code}-{section} is already monitored!", [chat_id])
                    return
            
            # Add new section to existing course
            existing_course['sections'].append({"section": section, "crn": crn})
        else:
            # Create new course with section
            new_course = {
                "code": course_code,
                "sections": [{"section": section, "crn": crn}]
            }
            courses_data[department].append(new_course)
        
        if save_courses(courses_data):
            success_message = f"✅ Added {course_code}-{section} (CRN: {crn}) to {department}!"
            send_telegram_message(success_message)
        else:
            send_telegram_message("❌ Failed to save section", [chat_id])
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", [chat_id])

def remove_course_section(text, chat_id):
    """Remove a course section from monitoring"""
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
            removed = False
            for course in courses_data[department]:
                if course['code'] == course_code:
                    initial_count = len(course['sections'])
                    course['sections'] = [
                        s for s in course['sections']
                        if s['section'] != section
                    ]
                    if len(course['sections']) < initial_count:
                        removed = True
                    break
            
            if removed:
                # Remove empty courses
                courses_data[department] = [
                    course for course in courses_data[department]
                    if course['sections']  # Keep only courses with sections
                ]
                
                save_courses(courses_data)
                removal_message = f"✅ Removed {course_code}-{section}!"
                send_telegram_message(removal_message)
            else:
                send_telegram_message(f"❌ Section {course_code}-{section} not found", [chat_id])
        else:
            send_telegram_message(f"❌ No courses in {department}", [chat_id])
            
    except Exception as e:
        send_telegram_message(f"❌ Error: {str(e)}", [chat_id])

def send_help(chat_id):
    message = """🆘 <b>Help</b>

<b>Commands:</b>
/start - Start bot
/status - Bot status with section availability
/seats - Quick section status only
/courses - Show monitored sections
/addcourse - How to add sections
/remove [section] - Remove section
/help - This message

<b>Check Interval:</b> 10 seconds ⚡
<b>Section Status:</b>
🟢 = Seats available
🔴 = Full
⚫ = Unknown

<b>Focus:</b> Monitors individual course sections for openings"""
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
    """Main monitoring loop focused on sections"""
    logger.info("🚀 Starting SECTION monitoring bot...")
    
    # Start support threads
    keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
    keep_alive_thread.start()
    
    commands_thread = threading.Thread(target=handle_telegram_commands, daemon=True)
    commands_thread.start()
    
    # Send startup message with section info
    courses_data = load_courses()
    total_sections = sum(
        len(course['sections']) 
        for department in courses_data.values() 
        for course in department
    )
    
    startup_message = f"""🤖 <b>Section Monitor Started!</b>

<b>Monitoring:</b> {total_sections} sections
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Focus:</b> Individual section availability
<b>Status:</b> 🟢 ACTIVE

Use /seats to see current section status!"""

    send_telegram_message(startup_message)
    
    previous_available_sections = set()
    
    while True:
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _, _, check_count = app_state.get_status()
            logger.info(f"🔍 Section check #{check_count + 1} at {current_time}")
            
            # Check section availability
            available_sections = check_section_availability()
            
            # Track newly available sections
            current_identifiers = {
                f"{s['code']}-{s['section']}-{s['crn']}" 
                for s in available_sections
            }
            
            new_sections = current_identifiers - previous_available_sections
            
            # Send notifications for new available sections
            if new_sections:
                new_available = [
                    s for s in available_sections 
                    if f"{s['code']}-{s['section']}-{s['crn']}" in new_sections
                ]
                send_section_notification(new_available)
                logger.info(f"📤 Notified {len(new_available)} newly available sections")
            
            previous_available_sections = current_identifiers
            
            logger.info(f"✅ Check #{check_count + 1} completed. {len(available_sections)} sections available")
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
    
    logger.info("🔧 Starting section monitor with FIXED section matching")
    monitor_loop()
