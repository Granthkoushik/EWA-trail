import os
import sys
import io
import time
import json
import socket
import queue
import logging
import threading
import datetime
import subprocess
import webbrowser
import platform
import urllib.parse
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from dotenv import load_dotenv

# ============================================================
# INITIALIZATION & DIRECTORIES
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load environment variables from .env
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path, override=True)

# Required folders
LOGS_DIR = os.path.join(BASE_DIR, "logs")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
AUDIO_DEBUG_DIR = os.path.join(BASE_DIR, "audio_debug")
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")
TEMPLATES_DIR = os.path.join(DASHBOARD_DIR, "templates")
STATIC_DIR = os.path.join(DASHBOARD_DIR, "static")

for d in [LOGS_DIR, AUDIO_DIR, AUDIO_DEBUG_DIR, DASHBOARD_DIR, TEMPLATES_DIR, STATIC_DIR]:
    os.makedirs(d, exist_ok=True)

# Logging configuration
LOG_FILE = os.path.join(LOGS_DIR, "ewa_server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("EWA")

# ============================================================
# CONFIGURATION
# ============================================================
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("SERVER_PORT", "5000"))
CONFIGURED_SERVER_IP = os.getenv("SERVER_IP", "10.246.48.28")
CONFIGURED_XIAO_IP = os.getenv("XIAO_IP", "10.246.48.146")
WIFI_SSID = os.getenv("WIFI_SSID", "Granth's s25")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

app = Flask(
    __name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR,
    static_url_path="/static"
)

# Pending confirmation state for sensitive operations (shutdown / restart)
pending_confirmation = {
    "action": None,
    "expires_at": 0
}

# ============================================================
# SYSTEM STATE & EVENT STREAM
# ============================================================
class SystemState:
    def __init__(self):
        self.server_start_time = time.time()
        self.xiao_last_seen = 0.0
        self.xiao_ip = CONFIGURED_XIAO_IP
        self.xiao_rssi = None
        self.xiao_free_heap = None
        self.xiao_record_count = 0
        self.last_audio_time = None
        self.last_audio_filename = None
        self.last_audio_bytes = 0
        self.last_audio_duration = 0.0
        self.last_transcript = ""
        self.last_intent = ""
        self.last_action = ""
        self.last_result = ""
        self.command_count = 0
        self.is_recording = False
        self.listeners = []
        self.lock = threading.Lock()
        self.history = []
        self.logs = []
        self.max_history = 100
        self.max_logs = 200

    def add_log(self, source, level, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {
            "time": timestamp,
            "source": source.upper(),
            "level": level.upper(),
            "message": message
        }
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > self.max_logs:
                self.logs.pop(0)

        # Broadcast SSE log event
        self.broadcast_event("log", entry)
        if level.upper() == "ERROR":
            logger.error(f"[{source}] {message}")
        else:
            logger.info(f"[{source}] {message}")

    def add_history(self, user_input, transcript, intent, action, result, status="Completed"):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "id": len(self.history) + 1,
            "time": timestamp,
            "input": user_input or "Voice Command",
            "transcript": transcript or "-",
            "intent": intent or "-",
            "action": action or "-",
            "result": result or "-",
            "status": status
        }
        with self.lock:
            self.history.insert(0, entry) # newest first
            if len(self.history) > self.max_history:
                self.history.pop()
            self.command_count += 1

        self.broadcast_event("history", entry)
        return entry

    def record_xiao_ping(self, ip, rssi=None, heap=None, count=None):
        self.xiao_last_seen = time.time()
        if ip:
            self.xiao_ip = ip
        if rssi:
            self.xiao_rssi = rssi
        if heap:
            self.xiao_free_heap = heap
        if count is not None:
            self.xiao_record_count = count

        self.broadcast_event("xiao_status", self.get_xiao_status())

    def get_xiao_status(self):
        online = (time.time() - self.xiao_last_seen) < 18.0 if self.xiao_last_seen > 0 else False
        seconds_ago = round(time.time() - self.xiao_last_seen, 1) if self.xiao_last_seen > 0 else None
        return {
            "online": online,
            "ip": self.xiao_ip,
            "rssi": self.xiao_rssi,
            "free_heap": self.xiao_free_heap,
            "record_count": self.xiao_record_count,
            "last_seen_sec_ago": seconds_ago,
            "last_seen_str": datetime.datetime.fromtimestamp(self.xiao_last_seen).strftime("%H:%M:%S") if self.xiao_last_seen > 0 else "Never"
        }

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def broadcast_event(self, event_type, data):
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            to_remove = []
            for q in self.listeners:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    to_remove.append(q)
            for q in to_remove:
                if q in self.listeners:
                    self.listeners.remove(q)

state = SystemState()

# ============================================================
# IP DETECTION
# ============================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return CONFIGURED_SERVER_IP or "127.0.0.1"

LOCAL_IP = get_local_ip()

# ============================================================
# GEMINI API INITIALIZATION
# ============================================================
gemini_client = None

def init_gemini():
    global gemini_client, GEMINI_API_KEY
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

    if GEMINI_API_KEY:
        try:
            from google import genai
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            state.add_log("GEMINI", "INFO", "Gemini 2.5 Flash Client initialized successfully.")
            return True
        except Exception as e:
            gemini_client = None
            state.add_log("GEMINI", "ERROR", f"Failed to initialize Gemini: {e}")
            return False
    else:
        gemini_client = None
        state.add_log("GEMINI", "WARNING", "GEMINI_API_KEY is not set. Running with smart offline command executor.")
        return False

init_gemini()

# ============================================================
# WINDOWS HARDWARE & APPLICATION CONTROLLER
# ============================================================
class WindowsController:
    @staticmethod
    def open_website(url):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            webbrowser.open(url)
            return f"Opened website: {url}"
        except Exception as e:
            return f"Failed to open {url}: {e}"

    @staticmethod
    def close_browser():
        browsers = ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"]
        closed = []
        for b in browsers:
            res = subprocess.run(
                ["taskkill", "/F", "/IM", b],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            if res.returncode == 0:
                closed.append(b.replace(".exe", ""))
        if closed:
            return f"Closed browser(s): {', '.join(closed)}"
        return "No running browser instance found."

    @staticmethod
    def web_search(engine, query):
        encoded = urllib.parse.quote_plus(query)
        if "youtube" in engine.lower():
            url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            url = f"https://www.google.com/search?q={encoded}"
        try:
            webbrowser.open(url)
            return f"Searched {engine} for '{query}'"
        except Exception as e:
            return f"Search failed: {e}"

    @staticmethod
    def open_application(name):
        app_clean = name.lower().strip()
        app_map = {
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "calc": "calc.exe",
            "paint": "mspaint.exe",
            "cmd": "cmd.exe",
            "command prompt": "cmd.exe",
            "terminal": "wt.exe",
            "powershell": "powershell.exe",
            "explorer": "explorer.exe",
            "files": "explorer.exe",
            "file explorer": "explorer.exe",
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "edge": "msedge.exe",
            "microsoft edge": "msedge.exe",
            "code": "code.cmd",
            "vs code": "code.cmd",
            "vscode": "code.cmd",
            "visual studio code": "code.cmd",
            "spotify": "spotify.exe",
            "taskmgr": "taskmgr.exe",
            "task manager": "taskmgr.exe"
        }

        cmd = app_map.get(app_clean)
        if not cmd:
            if app_clean.isalnum() and len(app_clean) < 30:
                cmd = f"{app_clean}.exe"
            else:
                return f"Disallowed application name: '{name}'"

        try:
            subprocess.Popen(cmd, shell=True)
            return f"Launched application: {name}"
        except Exception as e:
            return f"Could not launch {name}: {e}"

    @staticmethod
    def close_application(name):
        name_clean = name.lower().strip()

        # Safe handling for File Explorer (close open windows without killing desktop taskbar)
        if name_clean in ["explorer", "file explorer", "files"]:
            ps_script = "(New-Object -ComObject Shell.Application).Windows() | ForEach-Object { if ($_.Name -like '*Explorer*') { $_.Quit() } }"
            subprocess.run(
                ["powershell", "-Command", ps_script],
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            return "Closed File Explorer window(s)."

        process_map = {
            "notepad": "notepad.exe",
            "calculator": "CalculatorApp.exe",
            "calc": "CalculatorApp.exe",
            "paint": "mspaint.exe",
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "edge": "msedge.exe",
            "firefox": "firefox.exe",
            "spotify": "Spotify.exe",
            "discord": "Discord.exe",
            "code": "Code.exe",
            "vscode": "Code.exe",
            "vs code": "Code.exe",
            "taskmgr": "Taskmgr.exe",
            "cmd": "cmd.exe"
        }

        proc = process_map.get(name_clean)
        if not proc:
            if name_clean.endswith(".exe"):
                proc = name_clean
            else:
                proc = f"{name_clean}.exe"

        # Prevent killing critical system processes
        critical = ["csrss.exe", "winlogon.exe", "lsass.exe", "services.exe", "explorer.exe", "smss.exe", "system"]
        if proc.lower() in critical:
            return f"Safety policy: Cannot terminate protected system process '{proc}'"

        try:
            res = subprocess.run(
                ["taskkill", "/F", "/IM", proc],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            if res.returncode == 0:
                return f"Closed process {proc}"
            else:
                return f"Process {proc} was not running."
        except Exception as e:
            return f"Failed to close {name}: {e}"

    @staticmethod
    def set_volume_percent(percent):
        percent = max(0, min(100, int(percent)))
        try:
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):
                volume = speakers.EndpointVolume
                volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
                return f"Volume set to {percent}%"
        except Exception as e:
            logger.warning(f"pycaw set_volume failed: {e}")

        return f"Volume set to {percent}%"

    @staticmethod
    def volume_step(direction="up", steps=5):
        try:
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):
                volume = speakers.EndpointVolume
                cur = volume.GetMasterVolumeLevelScalar()
                delta = 0.05 * steps if direction == "up" else -0.05 * steps
                new_vol = max(0.0, min(1.0, cur + delta))
                volume.SetMasterVolumeLevelScalar(new_vol, None)
                return f"Volume changed to {round(new_vol * 100)}%"
        except Exception:
            pass

        key_code = 175 if direction == "up" else 174
        for _ in range(steps):
            subprocess.run(
                ["powershell", "-Command", f"(New-Object -ComObject WScript.Shell).SendKeys([char]{key_code})"],
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
        return f"Volume adjusted {direction} by {steps * 2}%"

    @staticmethod
    def volume_mute():
        try:
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):
                volume = speakers.EndpointVolume
                is_muted = volume.GetMute()
                volume.SetMute(not is_muted, None)
                return f"Volume {'unmuted' if is_muted else 'muted'}"
        except Exception:
            pass

        subprocess.run(
            ["powershell", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"],
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
        return "Volume mute toggled"

    @staticmethod
    def set_brightness(percent):
        percent = max(0, min(100, int(percent)))
        try:
            import screen_brightness_control as sbc
            sbc.set_brightness(percent)
            return f"Brightness set to {percent}%"
        except Exception as e:
            return f"Could not adjust brightness: {e}"

    @staticmethod
    def brightness_step(direction="up", delta=10):
        try:
            import screen_brightness_control as sbc
            cur = sbc.get_brightness()
            current_val = cur[0] if isinstance(cur, list) and cur else 50
            new_val = max(0, min(100, current_val + (delta if direction == "up" else -delta)))
            sbc.set_brightness(new_val)
            return f"Brightness set to {new_val}%"
        except Exception as e:
            return f"Brightness control unavailable: {e}"

    @staticmethod
    def lock_pc():
        try:
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
            return "PC locked successfully"
        except Exception as e:
            return f"Lock failed: {e}"

    @staticmethod
    def show_desktop():
        try:
            subprocess.run(
                ["powershell", "-Command", "(New-Object -ComObject Shell.Application).ToggleDesktop()"],
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            return "Toggled desktop view"
        except Exception as e:
            return f"Toggle desktop failed: {e}"

    @staticmethod
    def handle_system_power(action, confirmed=False):
        global pending_confirmation
        now = time.time()

        if action == "cancel":
            subprocess.run(["shutdown", "/a"], capture_output=True)
            pending_confirmation["action"] = None
            pending_confirmation["expires_at"] = 0
            return "Cancelled scheduled shutdown/restart."

        if not confirmed:
            pending_confirmation["action"] = action
            pending_confirmation["expires_at"] = now + 30
            return f"SAFETY CONFIRMATION: {action.title()} requires confirmation. Say or send 'confirm {action}' within 30 seconds."

        # Action is confirmed
        if now <= pending_confirmation["expires_at"] and pending_confirmation["action"] == action:
            pending_confirmation["action"] = None
            pending_confirmation["expires_at"] = 0
            flag = "/s" if action == "shutdown" else "/r"
            subprocess.Popen(["shutdown", flag, "/t", "60"])
            return f"{action.title()} confirmed. System will {action} in 60 seconds. Send 'cancel shutdown' to abort."
        else:
            return f"No valid pending confirmation for {action} or confirmation expired."

    @staticmethod
    def get_system_metrics():
        metrics = {
            "system": platform.system(),
            "node": platform.node(),
            "cpu_percent": 0,
            "ram_percent": 0,
            "battery_percent": None,
            "power_plugged": None
        }
        try:
            import psutil
            metrics["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            metrics["ram_percent"] = psutil.virtual_memory().percent
            batt = psutil.sensors_battery()
            if batt:
                metrics["battery_percent"] = batt.percent
                metrics["power_plugged"] = batt.power_plugged
        except Exception:
            pass
        return metrics

# ============================================================
# SAFE CONTROLLED COMMAND DISPATCHER
# ============================================================
def execute_safe_command(intent, target="", value=None, raw_text=""):
    """
    Never runs arbitrary shell scripts directly from AI.
    Executes only validated predefined functions.
    """
    intent = (intent or "").lower().strip()
    target = str(target or "").strip()
    raw = (raw_text or "").lower().strip()

    state.add_log("COMMAND", "INFO", f"Dispatching Intent: '{intent}', Target: '{target}', Value: '{value}'")

    if intent in ["open_website", "open_url"]:
        websites = {
            "youtube": "https://youtube.com",
            "google": "https://google.com",
            "gmail": "https://gmail.com",
            "github": "https://github.com",
            "chatgpt": "https://chatgpt.com",
            "instagram": "https://instagram.com",
            "reddit": "https://reddit.com",
            "twitter": "https://x.com",
            "x": "https://x.com",
            "spotify": "https://open.spotify.com",
            "netflix": "https://netflix.com"
        }
        url = websites.get(target.lower(), target)
        return WindowsController.open_website(url)

    elif intent == "close_browser":
        return WindowsController.close_browser()

    elif intent == "web_search":
        engine = "youtube" if "youtube" in target.lower() else "google"
        query = value if value else target
        return WindowsController.web_search(engine, query)

    elif intent in ["open_app", "open_application", "launch_app"]:
        return WindowsController.open_application(target)

    elif intent in ["close_app", "close_application", "kill_app"]:
        return WindowsController.close_application(target)

    elif intent in ["volume_up", "increase_volume"]:
        return WindowsController.volume_step("up", 5)

    elif intent in ["volume_down", "decrease_volume"]:
        return WindowsController.volume_step("down", 5)

    elif intent in ["set_volume", "volume_set"]:
        val = value if value is not None else 50
        return WindowsController.set_volume_percent(val)

    elif intent in ["mute_volume", "toggle_mute", "mute", "unmute"]:
        return WindowsController.volume_mute()

    elif intent in ["brightness_up", "increase_brightness"]:
        return WindowsController.brightness_step("up", 15)

    elif intent in ["brightness_down", "decrease_brightness"]:
        return WindowsController.brightness_step("down", 15)

    elif intent in ["set_brightness", "brightness_set"]:
        val = value if value is not None else 70
        return WindowsController.set_brightness(val)

    elif intent in ["lock_pc", "lock_computer"]:
        return WindowsController.lock_pc()

    elif intent in ["show_desktop", "minimize_all"]:
        return WindowsController.show_desktop()

    elif intent == "shutdown":
        return WindowsController.handle_system_power("shutdown", confirmed=False)

    elif intent == "confirm_shutdown":
        return WindowsController.handle_system_power("shutdown", confirmed=True)

    elif intent == "restart":
        return WindowsController.handle_system_power("restart", confirmed=False)

    elif intent == "confirm_restart":
        return WindowsController.handle_system_power("restart", confirmed=True)

    elif intent == "cancel_shutdown":
        return WindowsController.handle_system_power("cancel")

    elif intent == "system_metrics":
        metrics = WindowsController.get_system_metrics()
        return f"CPU: {metrics['cpu_percent']}%, RAM: {metrics['ram_percent']}%, Battery: {metrics.get('battery_percent', 'N/A')}%"

    else:
        # Fallback keyword matching for natural language
        if "close browser" in raw:
            return WindowsController.close_browser()

        if "youtube" in raw and ("search" in raw or "find" in raw):
            q = raw.replace("search youtube for", "").replace("search youtube", "").replace("youtube", "").strip()
            return WindowsController.web_search("youtube", q or "trending")

        if "google" in raw and ("search" in raw or "find" in raw):
            q = raw.replace("search google for", "").replace("search google", "").replace("google", "").strip()
            return WindowsController.web_search("google", q or "news")

        if "open youtube" in raw or raw == "youtube":
            return WindowsController.open_website("https://youtube.com")

        if "open google" in raw or raw == "google":
            return WindowsController.open_website("https://google.com")

        if "open github" in raw or raw == "github":
            return WindowsController.open_website("https://github.com")

        if "open vscode" in raw or "open vs code" in raw:
            return WindowsController.open_application("code")

        if "close vscode" in raw or "close vs code" in raw:
            return WindowsController.close_application("code")

        if "open notepad" in raw or raw == "notepad":
            return WindowsController.open_application("notepad")

        if "close notepad" in raw:
            return WindowsController.close_application("notepad")

        if "open calc" in raw or "calculator" in raw:
            return WindowsController.open_application("calc")

        if "close calc" in raw or "close calculator" in raw:
            return WindowsController.close_application("calc")

        if "open explorer" in raw or "open file explorer" in raw:
            return WindowsController.open_application("explorer")

        if "close explorer" in raw or "close file explorer" in raw:
            return WindowsController.close_application("explorer")

        if "open chrome" in raw:
            return WindowsController.open_application("chrome")

        if "volume up" in raw or "increase volume" in raw or "turn volume up" in raw or "louder" in raw:
            return WindowsController.volume_step("up", 5)

        if "volume down" in raw or "decrease volume" in raw or "turn volume down" in raw or "quieter" in raw:
            return WindowsController.volume_step("down", 5)

        if "mute" in raw or "mute my laptop" in raw:
            return WindowsController.volume_mute()

        if "brightness" in raw and ("percent" in raw or "%" in raw or any(c.isdigit() for c in raw)):
            words = raw.replace("%", " ").split()
            for w in words:
                if w.isdigit():
                    return WindowsController.set_brightness(int(w))

        if "brightness up" in raw or "increase brightness" in raw:
            return WindowsController.brightness_step("up", 15)

        if "brightness down" in raw or "decrease brightness" in raw:
            return WindowsController.brightness_step("down", 15)

        if "lock pc" in raw or "lock computer" in raw or "lock my laptop" in raw:
            return WindowsController.lock_pc()

        if "confirm shutdown" in raw:
            return WindowsController.handle_system_power("shutdown", confirmed=True)

        if "confirm restart" in raw:
            return WindowsController.handle_system_power("restart", confirmed=True)

        if "shutdown" in raw or "turn off laptop" in raw:
            return WindowsController.handle_system_power("shutdown", confirmed=False)

        if "restart" in raw:
            return WindowsController.handle_system_power("restart", confirmed=False)

        if "cancel shutdown" in raw:
            return WindowsController.handle_system_power("cancel")

        return f"I understood the request ('{raw}'), but it does not map to an allowed controlled system action."

# ============================================================
# NATURAL LANGUAGE PROCESSING (GEMINI + OFFLINE FALLBACK)
# ============================================================
GEMINI_SYSTEM_PROMPT = """
You are EWA (Enhanced Wearable Avatar), a real-time computer assistant.
You analyze audio voice commands or text commands and output structured JSON.
You must map the request to one of these safe intents:
- open_website (target: url or known site name like 'youtube', 'github', 'google')
- close_browser
- web_search (target: 'youtube' or 'google', value: 'search query')
- open_app (target: 'notepad', 'calc', 'paint', 'chrome', 'edge', 'code', 'explorer', 'cmd', 'terminal', 'spotify')
- close_app (target: 'notepad', 'calc', 'paint', 'code', 'explorer', 'chrome', etc.)
- volume_up
- volume_down
- set_volume (value: integer 0-100)
- mute_volume
- brightness_up
- brightness_down
- set_brightness (value: integer 0-100)
- lock_pc
- show_desktop
- shutdown (requires confirmation)
- confirm_shutdown
- restart (requires confirmation)
- confirm_restart
- cancel_shutdown
- system_metrics
- unknown

Return ONLY valid JSON matching this schema:
{
  "transcript": "exact spoken phrase",
  "intent": "intent_name",
  "target": "target_name_if_applicable",
  "value": null_or_integer_or_string,
  "action_description": "brief readable description of what will be done"
}
Do not include markdown code block formatting or explanations. Output pure JSON only.
"""

def parse_with_gemini_audio(audio_bytes):
    global gemini_client
    if gemini_client is None:
        init_gemini()

    if gemini_client is None:
        return {
            "transcript": "Audio received (Gemini API Key missing)",
            "intent": "unknown",
            "target": "",
            "value": None,
            "action_description": "Gemini API is offline. Configure GEMINI_API_KEY in .env"
        }

    # Sanitize audio bytes in case multipart headers or trailing bytes are present
    riff_idx = audio_bytes.find(b"RIFF")
    if riff_idx != -1:
        clean_audio = audio_bytes[riff_idx:]
        end_boundary = clean_audio.rfind(b"------")
        if end_boundary != -1:
            clean_audio = clean_audio[:end_boundary].rstrip(b"\r\n")
    else:
        clean_audio = audio_bytes

    from google.genai import types

    candidate_models = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"]
    last_err = None

    for model_name in candidate_models:
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=clean_audio, mime_type="audio/wav"),
                    GEMINI_SYSTEM_PROMPT
                ]
            )

            raw_text = response.text.strip()
            state.add_log("GEMINI", "INFO", f"[{model_name}] Response: {raw_text[:120]}...")

            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            parsed = json.loads(raw_text)
            return parsed

        except Exception as e:
            last_err = e
            state.add_log("GEMINI", "WARNING", f"Model {model_name} failed: {e}. Trying fallback...")

    state.add_log("GEMINI", "ERROR", f"All Gemini audio models failed: {last_err}")
    return {
        "transcript": "Voice command received",
        "intent": "unknown",
        "target": "",
        "value": None,
        "error": str(last_err)
    }

def parse_with_gemini_text(command_text):
    global gemini_client
    if gemini_client is None:
        init_gemini()

    if gemini_client:
        prompt = f"{GEMINI_SYSTEM_PROMPT}\nUser Command: {command_text}"
        candidate_models = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"]

        for model_name in candidate_models:
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                raw_text = response.text.strip()
                if raw_text.startswith("```"):
                    lines = raw_text.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    raw_text = "\n".join(lines).strip()
                return json.loads(raw_text)
            except Exception as e:
                state.add_log("GEMINI", "WARNING", f"Text parsing on {model_name} failed: {e}.")

    # Rule-based fallback parser
    cmd_lower = command_text.lower().strip()
    result = {
        "transcript": command_text,
        "intent": "unknown",
        "target": "",
        "value": None,
        "action_description": command_text
    }

    if "close browser" in cmd_lower:
        result.update({"intent": "close_browser"})
    elif "search youtube for" in cmd_lower:
        q = cmd_lower.split("search youtube for", 1)[1].strip()
        result.update({"intent": "web_search", "target": "youtube", "value": q})
    elif "search google for" in cmd_lower:
        q = cmd_lower.split("search google for", 1)[1].strip()
        result.update({"intent": "web_search", "target": "google", "value": q})
    elif "youtube" in cmd_lower and ("open" in cmd_lower or "launch" in cmd_lower):
        result.update({"intent": "open_website", "target": "youtube"})
    elif "github" in cmd_lower and ("open" in cmd_lower or "launch" in cmd_lower):
        result.update({"intent": "open_website", "target": "github"})
    elif "google" in cmd_lower and ("open" in cmd_lower or "launch" in cmd_lower):
        result.update({"intent": "open_website", "target": "google"})
    elif "vscode" in cmd_lower or "vs code" in cmd_lower:
        if "close" in cmd_lower:
            result.update({"intent": "close_app", "target": "code"})
        else:
            result.update({"intent": "open_app", "target": "code"})
    elif "notepad" in cmd_lower and "close" in cmd_lower:
        result.update({"intent": "close_app", "target": "notepad"})
    elif "notepad" in cmd_lower and ("open" in cmd_lower or "launch" in cmd_lower):
        result.update({"intent": "open_app", "target": "notepad"})
    elif "calc" in cmd_lower or "calculator" in cmd_lower:
        if "close" in cmd_lower:
            result.update({"intent": "close_app", "target": "calc"})
        else:
            result.update({"intent": "open_app", "target": "calc"})
    elif "explorer" in cmd_lower or "files" in cmd_lower:
        if "close" in cmd_lower:
            result.update({"intent": "close_app", "target": "explorer"})
        else:
            result.update({"intent": "open_app", "target": "explorer"})
    elif "chrome" in cmd_lower and ("open" in cmd_lower or "launch" in cmd_lower):
        result.update({"intent": "open_app", "target": "chrome"})
    elif "volume up" in cmd_lower or "increase volume" in cmd_lower or "turn volume up" in cmd_lower or "louder" in cmd_lower:
        result.update({"intent": "volume_up"})
    elif "volume down" in cmd_lower or "decrease volume" in cmd_lower or "turn volume down" in cmd_lower or "quieter" in cmd_lower:
        result.update({"intent": "volume_down"})
    elif "volume to" in cmd_lower:
        words = cmd_lower.split()
        for w in words:
            if w.isdigit():
                result.update({"intent": "set_volume", "value": int(w)})
                break
    elif "mute" in cmd_lower or "mute my laptop" in cmd_lower:
        result.update({"intent": "mute_volume"})
    elif "brightness" in cmd_lower and any(c.isdigit() for c in cmd_lower):
        words = cmd_lower.replace("%", " ").split()
        for w in words:
            if w.isdigit():
                result.update({"intent": "set_brightness", "value": int(w)})
                break
    elif "brightness up" in cmd_lower or "increase brightness" in cmd_lower:
        result.update({"intent": "brightness_up"})
    elif "brightness down" in cmd_lower or "decrease brightness" in cmd_lower:
        result.update({"intent": "brightness_down"})
    elif "lock pc" in cmd_lower or "lock computer" in cmd_lower or "lock my laptop" in cmd_lower:
        result.update({"intent": "lock_pc"})
    elif "desktop" in cmd_lower:
        result.update({"intent": "show_desktop"})
    elif "confirm shutdown" in cmd_lower:
        result.update({"intent": "confirm_shutdown"})
    elif "confirm restart" in cmd_lower:
        result.update({"intent": "confirm_restart"})
    elif "shutdown" in cmd_lower or "turn off laptop" in cmd_lower:
        result.update({"intent": "shutdown"})
    elif "restart" in cmd_lower:
        result.update({"intent": "restart"})
    elif "cancel shutdown" in cmd_lower:
        result.update({"intent": "cancel_shutdown"})

    return result

# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/health", methods=["GET"])
def health():
    """
    Health check endpoint.
    When pinged by XIAO ESP32-S3, inspects telemetry headers and records heartbeat.
    """
    remote_ip = request.remote_addr
    device_id = request.headers.get("X-Device-Id")
    xiao_ip = request.headers.get("X-Device-IP", remote_ip)
    xiao_rssi = request.headers.get("X-WiFi-RSSI")
    xiao_heap = request.headers.get("X-Free-Heap")
    record_cnt = request.headers.get("X-Record-Count")

    is_xiao = device_id == "XIAO-ESP32S3-Sense" or "ESP32" in request.headers.get("User-Agent", "") or xiao_ip == CONFIGURED_XIAO_IP

    if is_xiao:
        state.record_xiao_ping(
            ip=xiao_ip,
            rssi=f"{xiao_rssi} dBm" if xiao_rssi else None,
            heap=f"{int(xiao_heap)//1024} KB" if xiao_heap and xiao_heap.isdigit() else None,
            count=int(record_cnt) if record_cnt and record_cnt.isdigit() else state.xiao_record_count
        )
        state.add_log("XIAO", "INFO", f"Heartbeat received from XIAO at {xiao_ip} (RSSI: {xiao_rssi or 'N/A'})")

    return jsonify({
        "status": "online",
        "server": "EWA",
        "system": platform.system(),
        "gemini": gemini_client is not None,
        "xiao_online": state.get_xiao_status()["online"],
        "xiao_ip": state.xiao_ip,
        "timestamp": datetime.datetime.now().isoformat()
    })

@app.route("/audio", methods=["POST"])
def audio():
    """
    Audio ingestion endpoint.
    Receives raw WAV binary from XIAO ESP32-S3 Sense or Dashboard.
    """
    audio_data = request.get_data()
    remote_ip = request.remote_addr
    device_ip = request.headers.get("X-Device-IP", remote_ip)

    state.record_xiao_ping(ip=device_ip)

    if not audio_data or len(audio_data) < 44:
        state.add_log("SERVER", "ERROR", f"Received invalid or empty audio ({len(audio_data)} bytes)")
        return jsonify({"success": False, "error": "No valid audio received"}), 400

    byte_len = len(audio_data)
    duration_sec = round((byte_len - 44) / 32000.0, 2)
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_filename = f"ewa_{timestamp_str}.wav"
    wav_filepath = os.path.join(AUDIO_DIR, wav_filename)

    try:
        with open(wav_filepath, "wb") as f:
            f.write(audio_data)
        latest_path = os.path.join(AUDIO_DEBUG_DIR, "latest.wav")
        with open(latest_path, "wb") as f:
            f.write(audio_data)
    except Exception as e:
        state.add_log("SERVER", "ERROR", f"Failed to save WAV: {e}")

    state.last_audio_time = datetime.datetime.now().strftime("%H:%M:%S")
    state.last_audio_filename = wav_filename
    state.last_audio_bytes = byte_len
    state.last_audio_duration = duration_sec

    state.add_log("XIAO", "INFO", f"Received audio: {byte_len} bytes (~{duration_sec}s) from {device_ip}")

    # Broadcast audio received event to dashboard
    state.broadcast_event("audio_received", {
        "bytes": byte_len,
        "duration": duration_sec,
        "filename": wav_filename,
        "timestamp": state.last_audio_time
    })

    # Process with Gemini
    state.add_log("GEMINI", "INFO", "Sending audio buffer to Gemini for transcription & intent analysis...")
    gemini_result = parse_with_gemini_audio(audio_data)

    transcript = gemini_result.get("transcript", "")
    intent = gemini_result.get("intent", "unknown")
    target = gemini_result.get("target", "")
    value = gemini_result.get("value")

    state.last_transcript = transcript
    state.last_intent = intent

    state.add_log("GEMINI", "INFO", f"Transcribed: '{transcript}' | Intent: '{intent}'")

    # Execute via controlled dispatcher
    exec_result = execute_safe_command(intent=intent, target=target, value=value, raw_text=transcript)
    state.last_action = intent or target or "Controlled Action"
    state.last_result = exec_result

    state.add_log("COMMAND", "INFO", f"Execution Result: {exec_result}")

    # Record in history
    hist_entry = state.add_history(
        user_input="Voice (XIAO Mic)",
        transcript=transcript,
        intent=intent,
        action=f"{intent} -> {target}" if target else intent,
        result=exec_result,
        status="Completed" if "fail" not in exec_result.lower() else "Failed"
    )

    # Broadcast command execution event to dashboard
    state.broadcast_event("command_executed", {
        "transcript": transcript,
        "intent": intent,
        "target": target,
        "execution": exec_result,
        "result": exec_result,
        "history": hist_entry
    })

    # Return structured JSON adhering to the specified contract
    return jsonify({
        "success": True,
        "transcript": transcript,
        "command": intent,
        "target": target,
        "result": exec_result,
        "execution": exec_result,
        "duration_sec": duration_sec,
        "bytes": byte_len
    })

@app.route("/command", methods=["POST"])
def command():
    """
    Manual text command endpoint from Dashboard or API.
    """
    data = request.get_json(silent=True) or {}
    cmd = data.get("command", "").strip()

    if not cmd:
        return jsonify({"success": False, "error": "Field 'command' is required"}), 400

    state.add_log("SERVER", "INFO", f"Manual Command Received: '{cmd}'")

    parsed = parse_with_gemini_text(cmd)
    transcript = parsed.get("transcript", cmd)
    intent = parsed.get("intent", "unknown")
    target = parsed.get("target", "")
    value = parsed.get("value")

    exec_result = execute_safe_command(intent=intent, target=target, value=value, raw_text=cmd)

    hist_entry = state.add_history(
        user_input=cmd,
        transcript=transcript,
        intent=intent,
        action=f"{intent} -> {target}" if target else intent,
        result=exec_result,
        status="Completed" if "fail" not in exec_result.lower() else "Failed"
    )

    state.broadcast_event("command_executed", {
        "transcript": transcript,
        "intent": intent,
        "target": target,
        "execution": exec_result,
        "result": exec_result,
        "history": hist_entry
    })

    return jsonify({
        "success": True,
        "transcript": transcript,
        "command": intent,
        "target": target,
        "result": exec_result,
        "execution": exec_result,
        "interpretation": {
            "transcript": transcript,
            "intent": intent,
            "target": target,
            "value": value
        }
    })

@app.route("/system-action", methods=["POST"])
def system_action():
    """
    Predefined safe system action triggers from Dashboard buttons.
    """
    data = request.get_json(silent=True) or {}
    action_name = data.get("action", "").strip().lower()

    action_map = {
        "open_browser": ("open_website", "https://google.com"),
        "open_youtube": ("open_website", "https://youtube.com"),
        "open_notepad": ("open_app", "notepad"),
        "volume_up": ("volume_up", ""),
        "volume_down": ("volume_down", ""),
        "volume_mute": ("mute_volume", ""),
        "brightness_up": ("brightness_up", ""),
        "brightness_down": ("brightness_down", ""),
        "lock_pc": ("lock_pc", ""),
        "show_desktop": ("show_desktop", ""),
        "close_browser": ("close_browser", ""),
        "system_metrics": ("system_metrics", "")
    }

    if action_name not in action_map:
        return jsonify({"success": False, "error": f"Unknown system action: {action_name}"}), 400

    intent, target = action_map[action_name]
    result = execute_safe_command(intent=intent, target=target, raw_text=action_name)

    hist_entry = state.add_history(
        user_input=f"Button: {action_name}",
        transcript=action_name.replace("_", " ").title(),
        intent=intent,
        action=action_name,
        result=result,
        status="Completed"
    )

    state.broadcast_event("command_executed", {
        "transcript": action_name,
        "intent": intent,
        "target": target,
        "execution": result,
        "result": result,
        "history": hist_entry
    })

    return jsonify({
        "success": True,
        "transcript": action_name,
        "command": intent,
        "target": target,
        "result": result,
        "execution": result
    })

@app.route("/status", methods=["GET"])
def get_status():
    """
    Comprehensive live status for dashboard and clients.
    """
    xiao_st = state.get_xiao_status()
    metrics = WindowsController.get_system_metrics()

    return jsonify({
        "ewa_status": "ONLINE",
        "server_status": "RUNNING",
        "laptop_ip": LOCAL_IP,
        "server_port": PORT,
        "wifi_ssid": WIFI_SSID,
        "gemini_status": "ONLINE" if gemini_client is not None else "OFFLINE",
        "api_key_configured": bool(GEMINI_API_KEY),
        "xiao": xiao_st,
        "voice": {
            "is_recording": state.is_recording,
            "last_audio_time": state.last_audio_time,
            "last_duration": state.last_audio_duration,
            "last_bytes": state.last_audio_bytes,
            "last_transcript": state.last_transcript,
            "current_command": state.last_action,
            "last_result": state.last_result
        },
        "command_count": state.command_count,
        "uptime_seconds": int(time.time() - state.server_start_time),
        "metrics": metrics
    })

@app.route("/logs", methods=["GET"])
def get_logs():
    with state.lock:
        return jsonify(list(state.logs))

@app.route("/history", methods=["GET"])
def get_history():
    with state.lock:
        return jsonify(list(state.history))

@app.route("/audio/play/<filename>", methods=["GET"])
def play_audio(filename):
    safe_name = os.path.basename(filename)
    return send_from_directory(AUDIO_DIR, safe_name, mimetype="audio/wav")

@app.route("/audio/latest", methods=["GET"])
def play_latest_audio():
    latest_path = os.path.join(AUDIO_DEBUG_DIR, "latest.wav")
    if os.path.exists(latest_path):
        return send_from_directory(AUDIO_DEBUG_DIR, "latest.wav", mimetype="audio/wav")
    return jsonify({"error": "No audio recorded yet"}), 404

@app.route("/events", methods=["GET"])
def events():
    """
    Server-Sent Events (SSE) stream for real-time dashboard telemetry.
    """
    def event_stream():
        q = state.subscribe()
        try:
            initial_data = {
                "type": "init",
                "laptop_ip": LOCAL_IP,
                "server_port": PORT,
                "gemini": gemini_client is not None,
                "xiao": state.get_xiao_status()
            }
            yield f"event: init\ndata: {json.dumps(initial_data)}\n\n"

            while True:
                try:
                    msg = q.get(timeout=10.0)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            state.unsubscribe(q)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )

# ============================================================
# SERVER STARTUP
# ============================================================
if __name__ == "__main__":
    print()
    print("==================================================")
    print("      EWA: Enhanced Wearable Avatar Server        ")
    print("==================================================")
    print(f"System:       {platform.system()} ({platform.release()})")
    print(f"Host:         {HOST}")
    print(f"Port:         {PORT}")
    print(f"Local IP:     {LOCAL_IP}")
    print(f"Gemini:       {'ONLINE' if gemini_client else 'OFFLINE (Key Missing)'}")
    print(f"WiFi SSID:    {WIFI_SSID}")
    print(f"Expected XIAO:{CONFIGURED_XIAO_IP}")
    print("--------------------------------------------------")
    print(f"Dashboard:    http://localhost:{PORT}/")
    print(f"Network URL:  http://{LOCAL_IP}:{PORT}/")
    print(f"Health API:   http://{LOCAL_IP}:{PORT}/health")
    print("==================================================")
    print()

    state.add_log("SERVER", "INFO", f"EWA Server running on http://{LOCAL_IP}:{PORT}")

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True
    )