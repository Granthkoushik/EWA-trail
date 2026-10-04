# EWA: Enhanced Wearable Avatar

EWA is a voice-controlled wearable avatar system integrating the **Seeed Studio XIAO ESP32-S3 Sense** microcontroller with a Windows laptop server powered by **Google Gemini 2.5 Flash** and an interactive cybernetic dark-mode dashboard.

---

## Architecture

```
[Seeed Studio XIAO ESP32-S3 Sense]
Built-in PDM mic (GPIO 42/41)
            ↓
Record voice (16 kHz, 16-bit Mono WAV)
            ↓
WiFi HTTP POST /audio
            ↓
[ewa_server.py on Windows Laptop]
            ↓
Google Gemini API (or Smart Rule-Based Engine)
Speech → text → understand intent
            ↓
Safe Command Execution Layer
Whitelisted Windows applications, audio, brightness, web
            ↓
Interactive Dark Dashboard
Live telemetry, waveform, scrolling logs & history
```

---

## Project Structure

```
C:\EWA-Core\EWA\
│
├── ewa_server.py         # Main Flask server, API endpoints, SSE stream, Gemini & Windows execution
├── run.bat               # PRIMARY SINGLE-COMMAND LAUNCHER (Starts server & opens dashboard)
├── start_ewa.bat         # Alias batch launcher
├── requirements.txt      # Python dependencies
├── .env                  # Environment configuration (WiFi, Server IP, Gemini API Key)
├── .env.example          # Environment template
├── README.md             # Complete documentation and API contract
├── ewa.ino               # XIAO ESP32-S3 Sense Arduino firmware (Arduino-ESP32 3.3.x compatible)
│
├── firmware\
│   └── ewa.ino           # Synced firmware copy
│
├── ewa\
│   └── ewa.ino           # Arduino IDE folder-matched copy
│
├── dashboard\
│   ├── templates\
│   │   └── index.html    # Modern interactive dark console template
│   └── static\
│       ├── style.css     # Cyberpunk dark glassmorphic stylesheet
│       └── app.js        # SSE real-time updates, waveform visualizer, log streaming
│
├── logs\
│   └── ewa_server.log    # Persistent server logs
│
├── audio\                # Stored timestamped recordings from XIAO
└── audio_debug\
    └── latest.wav        # Latest audio file for playback and inspection
```

---

## Hardware Specifications & Pinout

### Seeed Studio XIAO ESP32-S3 Sense
* **Microcontroller:** ESP32-S3 (Xtensa Dual-Core 240MHz)
* **Microphone:** Built-in PDM digital microphone on expansion board
  * **PDM CLK:** `GPIO 42`
  * **PDM DATA:** `GPIO 41`
* **Status LED:** Yellow user LED on `GPIO 21` (Active **LOW**: `LOW` = illuminated, `HIGH` = off)
* **Audio Format:** 16,000 Hz sample rate, 16-bit PCM, Single Channel (Mono)
* **Memory Architecture:** Audio buffer is allocated dynamically in internal SRAM (~112 KB for 3.5 seconds). **No PSRAM dependency**.

---

## Quick Start (One Command)

To run the complete laptop server and launch the interactive dashboard:

```cmd
C:\EWA-Core\EWA\run.bat
```

This single command automatically:
1. Verifies Python 3.10+
2. Generates `.env` from template if not present
3. Verifies and installs all required dependencies quietly
4. Creates necessary runtime directories (`logs/`, `audio/`, `audio_debug/`)
5. Starts `ewa_server.py` on `0.0.0.0:5000`
6. Automatically opens your default web browser to `http://localhost:5000/`

---

## Supported Commands

### 1. Web
* "open YouTube" / "go to YouTube"
* "open Google"
* "open arbitrary website" (e.g., "open github.com", "open chatgpt")
* "search youtube for [query]" (e.g., "search youtube for robotics")
* "search google for [query]"
* "close browser" (safely terminates Chrome, Edge, or Firefox)

### 2. Applications
* "open notepad" / "close notepad"
* "open vscode" / "close vscode"
* "open calculator" / "close calculator"
* "open file explorer" / "close file explorer" (safely closes explorer windows without killing desktop taskbar)
* Extensible to Paint, Terminal, Spotify, Task Manager, etc.

### 3. System
* "volume up" / "turn volume up" / "increase volume"
* "volume down" / "decrease volume" / "quieter"
* "set volume to 50" / "volume 80"
* "mute my laptop" / "mute" / "unmute"
* "make brightness 50 percent" / "brightness 80%"
* "brightness up" / "brightness down"
* "lock my laptop" / "lock pc"
* "show desktop" / "minimize all"
* "shutdown" / "restart" (Strictly protected by safety confirmation: system requires saying or sending "confirm shutdown" within 30 seconds).

---

## Configuration (`.env`)

Edit `C:\EWA-Core\EWA\.env`:

```ini
# Google Gemini API Key (https://aistudio.google.com/)
GEMINI_API_KEY=

# 2.4GHz WiFi credentials for XIAO ESP32-S3
WIFI_SSID=Granth's s25
WIFI_PASSWORD=jayshreeram

# Laptop Server Network Information
SERVER_IP=10.246.48.28
SERVER_PORT=5000

# Expected XIAO IP Address
XIAO_IP=10.246.48.146
```

> **Note:** If `GEMINI_API_KEY` is not set, EWA automatically runs using an offline rule-based intent parser for standard commands. It will never crash or hang.

---

## Official API Contract

### 1. `GET /health` (Heartbeat & Telemetry)
* **Caller:** XIAO ESP32-S3 Sense
* **Headers:**
  * `X-Device-Id`: `XIAO-ESP32S3-Sense`
  * `X-Device-IP`: `<xiao_ip>`
  * `X-WiFi-RSSI`: `<rssi_dbm>`
  * `X-Free-Heap`: `<free_heap_bytes>`
  * `X-Record-Count`: `<total_recordings>`
* **Response (HTTP 200 JSON):**
  ```json
  {
    "status": "online",
    "server": "EWA",
    "system": "Windows",
    "gemini": true,
    "xiao_online": true,
    "xiao_ip": "10.246.48.146",
    "timestamp": "2026-09-30T16:40:00"
  }
  ```

### 2. `POST /audio` (Voice Upload & Execution)
* **Caller:** XIAO ESP32-S3 Sense or Web Client
* **Headers:**
  * `Content-Type`: `audio/wav`
  * `X-Device-Id`: `XIAO-ESP32S3-Sense`
  * `X-Device-IP`: `<xiao_ip>`
  * `X-Audio-Duration`: `3.5`
  * `X-Sample-Rate`: `16000`
* **Body:** Binary 44-byte RIFF WAV file with 16-bit mono 16 kHz PCM payload.
* **Server Action:**
  1. Saves recording to `audio/ewa_YYYYMMDD_HHMMSS.wav` and `audio_debug/latest.wav`.
  2. Broadcasts `audio_received` event to dashboard via SSE.
  3. Sends audio buffer to Gemini 2.5 Flash for transcription and intent extraction.
  4. Dispatches action through safe controlled command layer.
  5. Records in command history and broadcasts `command_executed` event.
* **Response (HTTP 200 JSON):**
  ```json
  {
    "success": true,
    "transcript": "open youtube",
    "command": "open_website",
    "target": "youtube",
    "result": "Opened website: https://youtube.com"
  }
  ```

### 3. `POST /command` (Interactive Text Command)
* **Caller:** Dashboard console or external HTTP client
* **Request Body:**
  ```json
  {
    "command": "search youtube for robotics"
  }
  ```
* **Response (HTTP 200 JSON):**
  ```json
  {
    "success": true,
    "transcript": "search youtube for robotics",
    "command": "web_search",
    "target": "youtube",
    "result": "Searched youtube for 'robotics'"
  }
  ```

### 4. `POST /system-action` (Predefined Safe Triggers)
* **Caller:** Dashboard control buttons
* **Request Body:** `{"action": "volume_up | volume_down | volume_mute | brightness_up | brightness_down | lock_pc | open_notepad | open_browser | open_youtube | show_desktop"}`
* **Response (HTTP 200 JSON):**
  ```json
  {
    "success": true,
    "action": "open_youtube",
    "result": "Opened website: https://youtube.com"
  }
  ```

### 5. `GET /events` (Server-Sent Events)
* Streams real-time updates for telemetry, live scrolling logs, audio waveform animations, and command history.
