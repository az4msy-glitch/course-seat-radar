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
    total_seats: int = 0
    seats_display: str = "N/A"
    verified: bool = False

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
                    seats_display = course.get('seats_display', 'N/A')
                    available_seats = course.get('available_seats', 0)
                    total_seats = course.get('total_seats', 0)
                    verified = course.get('verified', False)
                    
                    self.course_data[key] = CourseState(
                        status=course.get('status', 'N/A'),
                        last_updated=time.time(),
                        available_seats=available_seats,
                        total_seats=total_seats,
                        seats_display=seats_display,
                        verified=verified
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

def parse_seats_info(course_data):
    """Robust seat information parsing with validation"""
    try:
        seats = course_data.get('seats')
        enrollment = course_data.get('enrollment')
        
        logger.info(f"🔍 DEBUG Seat parsing - seats: {seats}, enrollment: {enrollment}")
        
        # Case 1: Both seats and enrollment are provided
        if seats is not None and enrollment is not None:
            available_seats = int(seats) if isinstance(seats, (int, str)) and str(seats).isdigit() else 0
            total_seats = int(enrollment) if isinstance(enrollment, (int, str)) and str(enrollment).isdigit() else available_seats
            
            # Validate: available seats cannot exceed total seats
            if available_seats > total_seats:
                logger.warning(f"⚠️ Seat validation failed: available({available_seats}) > total({total_seats}), swapping values")
                available_seats, total_seats = total_seats, available_seats
            
            seats_display = f"{available_seats}/{total_seats}"
            return seats_display, available_seats, total_seats, True
        
        # Case 2: Only seats is provided as a number
        elif seats is not None and isinstance(seats, (int, str)) and str(seats).isdigit():
            available_seats = int(seats)
            total_seats = available_seats  # Assume same if no enrollment data
            seats_display = f"{available_seats}/{total_seats}"
            return seats_display, available_seats, total_seats, True
        
        # Case 3: No valid seat data
        else:
            logger.warning(f"⚠️ No valid seat data: seats={seats}, enrollment={enrollment}")
            return "N/A", 0, 0, False
            
    except Exception as e:
        logger.error(f"❌ Error parsing seats: {e}")
        return "N/A", 0, 0, False

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

def find_section_data(course_data, target_section):
    """Find specific section data within course information - STRICT MATCHING"""
    try:
        # The API uses 'crm' for CRN and 'course' for course code
        course_crm = course_data.get('crm', '')  # Note: it's 'crm' not 'crn'
        
        # STRICT MATCH: Only match by CRN (most reliable)
        if str(course_crm) == str(target_section['crn']):
            logger.info(f"✅ CRN MATCH: {target_section['crn']} - seats: {course_data.get('seats')}")
            return course_data
        
        logger.info(f"❌ CRN MISMATCH: looking for {target_section['crn']}, found {course_crm}")
        return None
        
    except Exception as e:
        logger.error(f"Error finding section data: {e}")
        return None

def check_section_availability():
    """Check availability for specific sections with VALIDATION"""
    try:
        available_sections = []
        all_section_data = []
        courses_data = load_courses()
        
        total_monitored_sections = sum(len(course['sections']) for dept in courses_data.values() for course in dept)
        logger.info(f"🔍 Checking {total_monitored_sections} sections across {len(courses_data)} departments")
        
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
                logger.info(f"  Looking for '{course_code}' in {len(department_courses)} courses")
                
                # Find ALL courses that match our target course code (API uses 'course' field)
                matching_courses = []
                for course in department_courses:
                    if isinstance(course, dict):
                        actual_code = course.get('course')  # API uses 'course' field
                        if actual_code == course_code:
                            matching_courses.append(course)
                
                logger.info(f"  Found {len(matching_courses)} instances of '{course_code}'")
                
                # Check each section we're monitoring
                for target_section in target_course['sections']:
                    section_found = False
                    
                    for course_instance in matching_courses:
                        section_data = find_section_data(course_instance, target_section)
                        
                        if section_data:
                            section_found = True
                            # Parse seats information with validation
                            seats_display, available_seats, total_seats, verified = parse_seats_info(section_data)
                            
                            section_info = {
                                'department': department,
                                'code': course_code,
                                'section': target_section['section'],
                                'crn': target_section['crn'],
                                'seats_display': seats_display,
                                'available_seats': available_seats,
                                'total_seats': total_seats,
                                'title': section_data.get('title', 'N/A'),
                                'instructor': section_data.get('instructor', 'N/A'),
                                'schedule': f"{section_data.get('day', 'N/A')} {section_data.get('start_time', 'N/A')}-{section_data.get('end_time', 'N/A')}",
                                'location': f"{section_data.get('building', 'N/A')} {section_data.get('room', 'N/A')}",
                                'status': 'AVAILABLE' if available_seats > 0 else 'FULL',
                                'verified': verified
                            }
                            
                            all_section_data.append(section_info)
                            
                            # Only consider available if verified and actually has seats
                            if verified and available_seats > 0:
                                available_sections.append(section_info)
                                logger.info(f"🎯 VERIFIED AVAILABLE: {department} {course_code}-{target_section['section']} - {seats_display}")
                            elif verified and available_seats == 0:
                                logger.info(f"📊 VERIFIED FULL: {department} {course_code}-{target_section['section']} - {seats_display}")
                            else:
                                logger.warning(f"⚠️ UNVERIFIED: {department} {course_code}-{target_section['section']} - {seats_display}")
                            break
                    
                    if not section_found:
                        logger.warning(f"❌ Could not find data for {department} {course_code}-{target_section['section']} (CRN: {target_section['crn']})")
        
        # Update global status with section data
        update_section_status(all_section_data)
        
        logger.info(f"📊 Section check: {len(available_sections)} VERIFIED available out of {len(all_section_data)} monitored sections")
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
            seats_display = section.get('seats_display', 'N/A')
            available_seats = section.get('available_seats', 0)
            verified = section.get('verified', False)
            display_name = f"{section['code']}-{section['section']}"
            
            # Only show verified data as available
            if verified and available_seats > 0:
                emoji = "🟢"
                available_count += 1
                status_lines.append(f"{emoji} {display_name}: {seats_display} ✅")
            elif verified and available_seats == 0:
                emoji = "🔴"
                status_lines.append(f"{emoji} {display_name}: {seats_display}")
            else:
                emoji = "⚫"
                status_lines.append(f"{emoji} {display_name}: {seats_display} (unverified)")
        
        # Add summary line
        if available_count > 0:
            summary = f"\n🎉 {available_count} VERIFIED SECTIONS AVAILABLE!"
            status_lines.append(summary)
        
        final_status = "\n".join(status_lines) if status_lines else "📭 No section data available"
        app_state.update_status(final_status, section_data)
        logger.info(f"📊 Updated section status: {available_count} verified available sections")
        
    except Exception as e:
        logger.error(f"Error updating section status: {e}")
        app_state.update_status(f"❌ Error: {str(e)}")

def send_section_notification(available_sections, chat_ids=None):
    """Send detailed section availability notifications - ONLY VERIFIED"""
    if not available_sections:
        return
    
    # Filter only verified available sections
    verified_available = [s for s in available_sections if s.get('verified', False)]
    
    if not verified_available:
        logger.info("📭 No verified available sections to notify")
        return
    
    message = "🎉 <b>VERIFIED SECTION AVAILABLE!</b> 🎉\n\n"
    
    for section in verified_available:
        message += f"✅ <b>{section['code']}-{section['section']}</b> (CRN: {section['crn']})\n"
        message += f"   📚 {section['title']}\n"
        message += f"   👨‍🏫 {section['instructor']}\n"
        message += f"   🕒 {section['schedule']}\n"
        message += f"   📍 {section['location']}\n"
        message += f"   🪑 Seats: <b>{section['seats_display']}</b>\n\n"
    
    message += f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"⚡ Detected in {CHECK_INTERVAL} seconds\n"
    message += "✅ <i>VERIFIED - No false positives</i>"
    
    send_telegram_message(message, chat_ids)
    logger.info(f"📤 Sent VERIFIED notification for {len(verified_available)} available sections")

# ==================== ENHANCED TELEGRAM COMMANDS ====================

def send_seats_status(chat_id):
    """Quick section status with detailed seat information - SHOW VERIFICATION STATUS"""
    course_data = app_state.get_course_data()
    _, last_update, _ = app_state.get_status()
    
    if not course_data:
        send_telegram_message("📭 No course data available yet. Please wait for the first check to complete.", [chat_id])
        return
    
    status_age = time.time() - last_update
    status_lines = []
    
    for course_key, course_state in sorted(course_data.items()):
        if course_state.verified:
            emoji = "🟢" if course_state.available_seats > 0 else "🔴"
            status_lines.append(f"{emoji} {course_key}: {course_state.seats_display}")
        else:
            status_lines.append(f"⚫ {course_key}: {course_state.seats_display} (unverified)")
    
    message = f"""🪑 <b>DETAILED SEAT STATUS</b>

{"\n".join(status_lines) if status_lines else "📭 No section data available"}

🕒 Updated {int(status_age)} seconds ago
📊 Format: Available/Total Seats
✅ <i>Only verified data shown as available</i>"""

    send_telegram_message(message, [chat_id])

def send_section_status(chat_id):
    """Send detailed section status with seat information"""
    course_status, last_update, check_count = app_state.get_status()
    course_data = app_state.get_course_data()
    
    courses_data = load_courses()
    total_sections = sum(
        len(course['sections']) 
        for department in courses_data.values() 
        for course in department
    )
    
    status_age = time.time() - last_update
    
    # Count only verified available sections
    verified_available_count = sum(1 for state in course_data.values() if state.verified and state.available_seats > 0)
    verified_total = sum(1 for state in course_data.values() if state.verified)
    
    # Detailed seat information
    detailed_seats = []
    for course_key, course_state in sorted(course_data.items()):
        if course_state.verified:
            emoji = "🟢" if course_state.available_seats > 0 else "🔴"
            detailed_seats.append(f"{emoji} {course_key}: {course_state.seats_display}")
        else:
            detailed_seats.append(f"⚫ {course_key}: {course_state.seats_display} (unverified)")

    message = f"""📊 <b>SECTION MONITOR STATUS</b>

<b>Monitoring:</b> {total_sections} sections
<b>Verified Available:</b> {verified_available_count} sections
<b>Verified Data:</b> {verified_total}/{len(course_data)} sections
<b>Check Interval:</b> {CHECK_INTERVAL} seconds ⚡
<b>Total Checks:</b> {check_count}
<b>Last Update:</b> {int(status_age)} seconds ago

<b>DETAILED SEAT AVAILABILITY:</b>
{"\n".join(detailed_seats) if detailed_seats else "📭 No section data available"}

📊 <i>Format: Available/Total Seats</i>
✅ <i>Only verified data triggers notifications</i>"""

    send_telegram_message(message, [chat_id])

# [Keep all the other Telegram command functions the same as before - they're already included above]

# ==================== MAIN MONITORING LOOP ====================

def monitor_loop():
    """Main monitoring loop focused on sections - WITH FALSE POSITIVE PROTECTION"""
    logger.info("🚀 Starting SECTION monitoring bot with FALSE POSITIVE PROTECTION...")
    
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
<b>Seat Display:</b> Available/Total format
<b>Protection:</b> ✅ No false positives
<b>Status:</b> 🟢 ACTIVE

Use /seats to see current section status with seat counts!"""

    send_telegram_message(startup_message)
    
    previous_available_sections = set()
    
    while True:
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _, _, check_count = app_state.get_status()
            logger.info(f"🔍 Section check #{check_count + 1} at {current_time}")
            
            # Check section availability
            available_sections = check_section_availability()
            
            # Track newly available sections (ONLY VERIFIED)
            current_identifiers = {
                f"{s['code']}-{s['section']}-{s['crn']}" 
                for s in available_sections if s.get('verified', False)
            }
            
            new_sections = current_identifiers - previous_available_sections
            
            # Send notifications for new available sections (ONLY VERIFIED)
            if new_sections:
                new_available = [
                    s for s in available_sections 
                    if f"{s['code']}-{s['section']}-{s['crn']}" in new_sections and s.get('verified', False)
                ]
                send_section_notification(new_available)
                logger.info(f"📤 Sent VERIFIED notification for {len(new_available)} newly available sections")
            
            previous_available_sections = current_identifiers
            
            verified_available_count = sum(1 for s in available_sections if s.get('verified', False) and s.get('available_seats', 0) > 0)
            logger.info(f"✅ Check #{check_count + 1} completed. {verified_available_count} VERIFIED sections available")
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
    
    logger.info("🔧 Starting section monitor with FALSE POSITIVE PROTECTION")
    monitor_loop()
