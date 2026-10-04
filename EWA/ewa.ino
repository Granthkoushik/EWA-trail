#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>

// ============================================================
// EWA (Enhanced Wearable Avatar) Firmware v2.5
// Target: Seeed Studio XIAO ESP32-S3 Sense
// Arduino-ESP32 Core: 3.3.x compatible (ESP_I2S API)
// Audio: 16 kHz, 16-bit, Mono PCM
// Zero-Allocation Failure: Static SRAM Buffer (No PSRAM needed)
// ============================================================

// ============================================================
// CONFIGURATION VARIABLES (Edit these for your network)
// ============================================================
const char* WIFI_SSID     = "Granth's s25";
const char* WIFI_PASSWORD = "jayshreeram";

// Laptop running ewa_server.py (Updated to current laptop IP)
const char* SERVER_IP     = "192.168.1.49";
const uint16_t SERVER_PORT = 5000;

// ============================================================
// HARDWARE PIN DEFINITIONS
// XIAO ESP32-S3 Sense Built-in PDM Microphone & LED
// ============================================================
#define MIC_CLK_PIN   42
#define MIC_DATA_PIN  41

// User LED on XIAO ESP32-S3 (Yellow LED, Active LOW: LOW=ON, HIGH=OFF)
#define LED_PIN       21

// ============================================================
// AUDIO RECORDING PARAMETERS
// 16 kHz, 16-bit, Mono PCM
// 3.0-second recording = 96,000 bytes PCM + 44 bytes header = 96,044 bytes
// Statically allocated in SRAM: 100% immune to heap fragmentation!
// ============================================================
#define SAMPLE_RATE       16000
#define BITS_PER_SAMPLE   16
#define CHANNELS          1

#define RECORD_SECONDS    3.0f
#define AUDIO_DATA_SIZE   ((uint32_t)(SAMPLE_RATE * RECORD_SECONDS * (BITS_PER_SAMPLE / 8)))
#define WAV_HEADER_SIZE   44
#define TOTAL_BUFFER_SIZE (WAV_HEADER_SIZE + AUDIO_DATA_SIZE)

// ============================================================
// GLOBALS & STATIC AUDIO BUFFER
// ============================================================
I2SClass I2S;
static uint8_t audioBuffer[TOTAL_BUFFER_SIZE]; // Guaranteed static allocation
uint32_t lastHealthPing = 0;
const uint32_t HEALTH_PING_INTERVAL_MS = 10000;
uint32_t sessionRecordCount = 0;

// ============================================================
// LED HELPERS (Active LOW on XIAO ESP32-S3)
// ============================================================
void ledOn()  { digitalWrite(LED_PIN, LOW); }
void ledOff() { digitalWrite(LED_PIN, HIGH); }

void ledBlink(int times, int delayMs) {
    for (int i = 0; i < times; i++) {
        ledOn();
        delay(delayMs);
        ledOff();
        delay(delayMs);
    }
}

// ============================================================
// WAV HEADER GENERATION (Little-Endian)
// ============================================================
static inline void writeLE16(uint8_t* p, uint16_t val) {
    p[0] = (uint8_t)(val & 0xFF);
    p[1] = (uint8_t)((val >> 8) & 0xFF);
}

static inline void writeLE32(uint8_t* p, uint32_t val) {
    p[0] = (uint8_t)(val & 0xFF);
    p[1] = (uint8_t)((val >> 8) & 0xFF);
    p[2] = (uint8_t)((val >> 16) & 0xFF);
    p[3] = (uint8_t)((val >> 24) & 0xFF);
}

void writeWavHeader(uint8_t* header, uint32_t dataBytes) {
    memset(header, 0, WAV_HEADER_SIZE);

    uint32_t fileLength = dataBytes + 36;
    uint16_t audioFormat = 1; // PCM
    uint16_t numChannels = CHANNELS;
    uint32_t sampleRate = SAMPLE_RATE;
    uint16_t bitsPerSample = BITS_PER_SAMPLE;
    uint16_t blockAlign = numChannels * (bitsPerSample / 8);
    uint32_t byteRate = sampleRate * blockAlign;

    memcpy(header + 0, "RIFF", 4);
    writeLE32(header + 4, fileLength);
    memcpy(header + 8, "WAVE", 4);

    memcpy(header + 12, "fmt ", 4);
    writeLE32(header + 16, 16);
    writeLE16(header + 20, audioFormat);
    writeLE16(header + 22, numChannels);
    writeLE32(header + 24, sampleRate);
    writeLE32(header + 28, byteRate);
    writeLE16(header + 32, blockAlign);
    writeLE16(header + 34, bitsPerSample);

    memcpy(header + 36, "data", 4);
    writeLE32(header + 40, dataBytes);
}

// ============================================================
// WIFI CONNECTION & RECONNECT
// ============================================================
bool connectWiFi() {
    Serial.println();
    Serial.println("========================================");
    Serial.println("           CONNECTING TO WIFI");
    Serial.println("========================================");
    Serial.printf("Target SSID: %s\n", WIFI_SSID);
    Serial.flush();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect(true);
    delay(200);

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startMs < 20000) {
        ledOn();
        delay(250);
        ledOff();
        delay(250);
        Serial.print(".");
        Serial.flush();
    }
    Serial.println();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[EWA-WIFI] FAILED to connect to WiFi! Retrying...");
        Serial.flush();
        return false;
    }

    Serial.println("[EWA-WIFI] WiFi Connected!");
    Serial.printf("[EWA-WIFI] XIAO IP Address: %s\n", WiFi.localIP().toString().c_str());
    Serial.printf("[EWA-WIFI] Gateway:         %s\n", WiFi.gatewayIP().toString().c_str());
    Serial.printf("[EWA-WIFI] RSSI:            %d dBm\n", WiFi.RSSI());
    Serial.printf("[EWA-WIFI] Target Server:   http://%s:%u\n", SERVER_IP, SERVER_PORT);
    Serial.flush();

    ledBlink(3, 100);
    return true;
}

// ============================================================
// MICROPHONE SETUP (Arduino-ESP32 3.3.x Compatible)
// ============================================================
bool setupMicrophone() {
    Serial.println();
    Serial.println("[EWA-MIC] Initializing PDM Microphone via ESP_I2S...");
    Serial.printf("[EWA-MIC] CLK Pin:  GPIO %d\n", MIC_CLK_PIN);
    Serial.printf("[EWA-MIC] DATA Pin: GPIO %d\n", MIC_DATA_PIN);
    Serial.printf("[EWA-MIC] Rate: %d Hz | Bits: %d | Channels: %d\n", SAMPLE_RATE, BITS_PER_SAMPLE, CHANNELS);
    Serial.flush();

    I2S.setPinsPdmRx(MIC_CLK_PIN, MIC_DATA_PIN);

    bool ok = I2S.begin(
        I2S_MODE_PDM_RX,
        SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_16BIT,
        I2S_SLOT_MODE_MONO
    );

    if (!ok) {
        Serial.println("[EWA-MIC] ERROR: I2S.begin failed!");
        Serial.flush();
        return false;
    }

    Serial.println("[EWA-MIC] PDM Microphone initialized successfully.");
    Serial.flush();
    return true;
}

// ============================================================
// SERVER HEALTH CHECK / HEARTBEAT
// ============================================================
bool checkServerHealth() {
    if (WiFi.status() != WL_CONNECTED) return false;

    HTTPClient http;
    String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/health";

    http.begin(url);
    http.setConnectTimeout(3000);
    http.setTimeout(3000);

    http.addHeader("X-Device-Id", "XIAO-ESP32S3-Sense");
    http.addHeader("X-Device-IP", WiFi.localIP().toString());
    http.addHeader("X-WiFi-RSSI", String(WiFi.RSSI()));
    http.addHeader("X-Free-Heap", String(ESP.getFreeHeap()));
    http.addHeader("X-Record-Count", String(sessionRecordCount));

    int code = http.GET();
    bool online = (code == 200);

    if (online) {
        Serial.printf("[EWA-HEALTH] Server Online at %s:%u (HTTP %d)\n", SERVER_IP, SERVER_PORT, code);
    } else {
        Serial.printf("[EWA-HEALTH] Server Unreachable at %s:%u (HTTP %d)\n", SERVER_IP, SERVER_PORT, code);
    }
    Serial.flush();

    http.end();
    return online;
}

// ============================================================
// AUDIO RECORDING (16 kHz, 16-bit Mono PCM)
// ============================================================
bool recordAudio(uint32_t& outBytes) {
    Serial.println();
    Serial.println("========================================");
    Serial.printf("   [EWA-REC] RECORDING (%.1f SECONDS)...   \n", RECORD_SECONDS);
    Serial.println("========================================");
    Serial.println(">> Speak command now into XIAO Sense...");
    Serial.flush();

    ledOn(); // LED ON during recording

    outBytes = 0;
    uint32_t targetBytes = AUDIO_DATA_SIZE;
    uint8_t* writePtr = audioBuffer + WAV_HEADER_SIZE;

    uint32_t startMs = millis();
    uint32_t maxDurationMs = (uint32_t)(RECORD_SECONDS * 1000.0f);

    // Flush any stale buffer
    for (int i = 0; i < 64; i++) {
        I2S.read();
    }

    while ((millis() - startMs < maxDurationMs) && (outBytes < targetBytes)) {
        int sample = I2S.read();
        if (sample < 0) {
            delayMicroseconds(40);
            continue;
        }

        int16_t pcm = (int16_t)sample;
        writePtr[outBytes]     = (uint8_t)(pcm & 0xFF);
        writePtr[outBytes + 1] = (uint8_t)((pcm >> 8) & 0xFF);
        outBytes += 2;
    }

    ledOff(); // LED OFF when recording ends

    Serial.printf("[EWA-REC] Completed: %u bytes captured (%.2f seconds)\n",
        outBytes, (float)outBytes / (SAMPLE_RATE * 2.0f));
    Serial.flush();

    return (outBytes > 0);
}

// ============================================================
// AUDIO TRANSMISSION & RESULT DISPLAY
// ============================================================
bool sendAudioToServer(uint32_t dataBytes) {
    Serial.println("[EWA-POST] Preparing WAV and sending to /audio...");
    Serial.flush();

    writeWavHeader(audioBuffer, dataBytes);
    uint32_t totalBytes = WAV_HEADER_SIZE + dataBytes;

    String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/audio";
    Serial.printf("[EWA-POST] Target URL: %s (%u bytes)\n", url.c_str(), totalBytes);
    Serial.flush();

    HTTPClient http;
    http.begin(url);
    http.setConnectTimeout(5000);
    http.setTimeout(25000); // Allow Gemini time to process

    http.addHeader("Content-Type", "audio/wav");
    http.addHeader("X-Device-Id", "XIAO-ESP32S3-Sense");
    http.addHeader("X-Device-IP", WiFi.localIP().toString());
    http.addHeader("X-Audio-Duration", String(RECORD_SECONDS, 1));
    http.addHeader("X-Sample-Rate", String(SAMPLE_RATE));

    ledOn();
    int httpResponse = http.POST(audioBuffer, totalBytes);
    ledOff();

    Serial.printf("[EWA-POST] HTTP Status: %d\n", httpResponse);
    Serial.flush();

    if (httpResponse >= 200 && httpResponse < 300) {
        String respBody = http.getString();
        sessionRecordCount++;

        Serial.println("\n========================================");
        Serial.println("           EWA SERVER RESPONSE          ");
        Serial.println("========================================");
        Serial.println(respBody);
        Serial.println("========================================\n");
        Serial.flush();

        ledBlink(2, 80);
        http.end();
        return true;
    } else {
        Serial.println("[EWA-POST] ERROR: Audio upload failed!");
        Serial.flush();
        http.end();
        return false;
    }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    pinMode(LED_PIN, OUTPUT);
    ledOff();

    Serial.begin(115200);
    delay(1500);

    Serial.println("\n");
    Serial.println("==================================================");
    Serial.println("     EWA: Enhanced Wearable Avatar Firmware v2.5  ");
    Serial.println("         Seeed Studio XIAO ESP32-S3 Sense         ");
    Serial.println("==================================================");
    Serial.printf("Chip Model:  %s (Rev %d)\n", ESP.getChipModel(), ESP.getChipRevision());
    Serial.printf("Free Heap:   %u bytes (%u KB)\n", ESP.getFreeHeap(), ESP.getFreeHeap() / 1024);
    Serial.printf("Buffer Size: %u bytes (Static SRAM)\n", TOTAL_BUFFER_SIZE);
    Serial.flush();

    // 1. Setup Built-in PDM Microphone
    if (!setupMicrophone()) {
        Serial.println("[FATAL] PDM Microphone Init Failed. Halting.");
        Serial.flush();
        while (true) { ledBlink(2, 200); delay(500); }
    }

    // 2. Connect to WiFi
    if (!connectWiFi()) {
        Serial.println("[WARNING] Initial WiFi connect failed. Retrying in loop...");
        Serial.flush();
    }

    // 3. Initial Health Check
    checkServerHealth();

    Serial.println("\n[EWA-MAIN] System initialized. Entering continuous loop.\n");
    Serial.flush();
}

// ============================================================
// CONTINUOUS RECORD & LISTEN LOOP
// ============================================================
void loop() {
    // 1. Check WiFi
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\n[EWA-LOOP] WiFi disconnected. Reconnecting...");
        Serial.flush();
        connectWiFi();
        delay(2000);
        return;
    }

    // 2. Periodic Heartbeat
    if (millis() - lastHealthPing > HEALTH_PING_INTERVAL_MS) {
        lastHealthPing = millis();
        checkServerHealth();
    }

    // 3. Check server health
    HTTPClient checkHttp;
    String healthUrl = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + "/health";
    checkHttp.begin(healthUrl);
    checkHttp.setConnectTimeout(2500);
    checkHttp.setTimeout(2500);
    int hCode = checkHttp.GET();
    checkHttp.end();

    if (hCode != 200) {
        Serial.printf("[EWA-LOOP] Waiting for EWA server at http://%s:%u (HTTP %d)...\n",
            SERVER_IP, SERVER_PORT, hCode);
        Serial.flush();
        delay(3000);
        return;
    }

    // 4. Record audio from built-in PDM mic
    uint32_t bytesCaptured = 0;
    bool recorded = recordAudio(bytesCaptured);

    if (recorded && bytesCaptured > 1000) {
        // 5. Send WAV audio to laptop server
        sendAudioToServer(bytesCaptured);
    } else {
        Serial.println("[EWA-LOOP] Recording buffer empty or error.");
        Serial.flush();
    }

    Serial.println("[EWA-LOOP] Standby 2.5 seconds before next interaction...");
    Serial.flush();
    delay(2500);
}
