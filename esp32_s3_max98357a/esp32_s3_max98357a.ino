// ESP32-S3 + MAX98357A -> plays replies from laptop HTTP server
// Boards: ESP32S3 Dev Module (Arduino-ESP32 core 2.x/3.x)
// Libraries (Library Manager):
//   ESP8266Audio by Earle Philhower
//   (no ArduinoJson needed — JSON parsed manually)
//
// Wiring (ESP32-S3 -> MAX98357A):
//   GPIO4  -> BCLK
//   GPIO5  -> LRC (WS)
//   GPIO6  -> DIN
//   GPIO7  -> SD (shutdown, HIGH=enable) — or tie SD to 3.3V and omit
//   GND    -> GND (MUST common-ground with 5V supply)
//   MAX98357A VIN -> external 5V (1A+). Don't power loud 4ohm/3W from ESP32 3.3V pin.
//   MAX98357A GND -> same GND
//   Speaker -> +/- terminals (4 ohm, 3W OK, ~3.2W max at 5V)
//   GAIN pin: leave FLOATING for 9dB (safest for 3W, least distortion).
//             Tie to GND for 12dB louder, to VDD for 15dB (clips easily).

#include <WiFi.h>
#include <HTTPClient.h>
#include "AudioFileSourcePROGMEM.h"
#include "AudioGeneratorMP3.h"
#include "AudioOutputI2S.h"

// ---------- EDIT THESE ----------
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char* SERVER    = "http://192.168.1.50:8000"; // <-- laptop IP from `ipconfig`, ESP32+phone+laptop same WiFi
// --------------------------------

#define I2S_BCLK 4
#define I2S_LRC  5
#define I2S_DOUT 6
#define I2S_SD   7   // set to -1 if you tied SD directly to 3.3V

AudioGeneratorMP3 *mp3 = nullptr;
AudioFileSourcePROGMEM *memsrc = nullptr;
uint8_t *audioData = nullptr;
AudioOutputI2S *out = nullptr;

int lastVersion = 0;
unsigned long lastPoll = 0;

void stopPlayback() {
  if (mp3) { mp3->stop(); delete mp3; mp3 = nullptr; }
  if (memsrc) { delete memsrc; memsrc = nullptr; }
  if (audioData) { free(audioData); audioData = nullptr; }
}

// Download full mp3 into RAM first, then play from memory.
// Fixes "first word cut off" caused by streaming underrun / I2S lock delay.
bool downloadAndPlay(const String &url, int ver) {
  stopPlayback();
  delay(20);

  HTTPClient dl;
  dl.setTimeout(8000);
  dl.begin(url);
  int c = dl.GET();
  if (c != 200) {
    Serial.printf("[dl] HTTP %d\n", c);
    dl.end();
    return false;
  }
  int len = dl.getSize(); // usually known via StaticFiles
  WiFiClient *s = dl.getStreamPtr();

  const size_t MAX_MP3 = 300000; // ~40 words is ~60-120KB, cap for safety
  uint8_t *buf = nullptr;
  size_t total = 0;

  if (len > 0) {
    if ((size_t)len > MAX_MP3) { Serial.printf("[dl] too big %d\n", len); dl.end(); return false; }
    buf = (uint8_t*) malloc(len);
    if (!buf) { Serial.println("[dl] malloc fail"); dl.end(); return false; }
    unsigned long t0 = millis();
    while (total < (size_t)len && millis() - t0 < 10000) {
      size_t avail = s->available();
      if (avail) {
        size_t n = s->readBytes(buf + total, min(avail, (size_t)len - total));
        total += n;
      } else {
        delay(5);
      }
    }
  } else {
    // chunked: grow buffer
    buf = (uint8_t*) malloc(MAX_MP3);
    if (!buf) { Serial.println("[dl] malloc fail"); dl.end(); return false; }
    unsigned long t0 = millis();
    while (dl.connected() && total < MAX_MP3 && millis() - t0 < 10000) {
      size_t avail = s->available();
      if (avail) {
        size_t n = s->readBytes(buf + total, min(avail, MAX_MP3 - total));
        total += n;
        t0 = millis();
      } else {
        // stream ended?
        if (!s->available() && !dl.connected()) break;
        delay(5);
      }
      if (total > 0 && !s->available()) { delay(100); if (!s->available()) break; }
    }
  }
  dl.end();

  if (total < 1000) { Serial.printf("[dl] too small %d\n", (int)total); free(buf); return false; }
  Serial.printf("[dl] v%d got %d bytes, heap %d\n", ver, (int)total, ESP.getFreeHeap());

  audioData = buf;
  memsrc = new AudioFileSourcePROGMEM(audioData, total);
  mp3 = new AudioGeneratorMP3();
  // Prime I2S/DMA with ~50ms silence so MAX98357A clock locks BEFORE first word
  {
    int16_t silence[2] = {0, 0};
    for (int i = 0; i < 800; i++) out->ConsumeSample(silence);
  }
  if (mp3->begin(memsrc, out)) {
    lastVersion = ver;
    return true;
  }
  Serial.println("[play] mp3 begin failed (bad data?)");
  stopPlayback();
  return false;
}

void reportPlayed(int v) {
  HTTPClient http;
  http.begin(String(SERVER) + "/api/played");
  http.addHeader("Content-Type", "application/json");
  http.POST("{\"version\":" + String(v) + "}");
  http.end();
}

int checkForNew() {
  HTTPClient http;
  http.setTimeout(4000);
  http.begin(String(SERVER) + "/api/next?last=" + String(lastVersion));
  int code = http.GET();
  if (code != 200) { Serial.printf("[poll] HTTP %d\n", code); http.end(); return -1; }
  String payload = http.getString();
  http.end();
  // Manual JSON parse (avoids ArduinoJson lib):
  // {"version":5,"audio_url":"/audio/reply-5.mp3","reply":"...","new":true}
  bool isNew = payload.indexOf("\"new\":true") >= 0 || payload.indexOf("\"new\": true") >= 0;
  int ver = lastVersion;
  int vi = payload.indexOf("\"version\"");
  if (vi >= 0) {
    int colon = payload.indexOf(':', vi);
    if (colon >= 0) ver = payload.substring(colon + 1).toInt();
  }
  String audioPath = "";
  int ai = payload.indexOf("\"audio_url\"");
  if (ai >= 0) {
    int c1 = payload.indexOf(':', ai);
    int q1 = payload.indexOf('"', c1);
    int q2 = payload.indexOf('"', q1 + 1);
    if (q1 >= 0 && q2 > q1) audioPath = payload.substring(q1 + 1, q2);
  }
  if (!(isNew && ver > lastVersion && audioPath.length() > 0)) return -1;

  String url = String(SERVER) + audioPath;
  Serial.printf("[new] v%d\n  %s\n", ver, url.c_str());

  // 1) Pre-check with HEAD (not GET): GET-then-close without reading the
  // ~200KB body makes uvicorn/asyncio log WinError 10054 (harmless but noisy).
  for (int attempt = 0; attempt < 3; attempt++) {
    HTTPClient chk;
    chk.setTimeout(4000);
    chk.begin(url);
    int c = chk.sendRequest("HEAD");
    int len = chk.getSize(); // -1 if unknown
    chk.end();
    if (c != 200) {
      Serial.printf("[chk] HTTP %d try %d, waiting...\n", c, attempt);
      delay(800);
      continue;
    }
    if (len > 0 && len < 1000) {
      Serial.printf("[chk] too small (%d), waiting...\n", len);
      delay(800);
      continue;
    }
    break; // ok (len==-1 is fine, edge-tts streams chunked sometimes)
  }

  // 2) Download fully, then play from RAM with retries. Only bump lastVersion on success.
  for (int attempt = 0; attempt < 3; attempt++) {
    if (downloadAndPlay(url, ver)) {
      Serial.printf("[play] v%d attempt %d ok\n", ver, attempt);
      return ver;
    }
    Serial.printf("[play] try %d failed, heap %d RSSI %d\n", attempt, ESP.getFreeHeap(), WiFi.RSSI());
    delay(800);
  }
  return -1; // keep old lastVersion so we retry next poll
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nESP32-S3 MAX98357A talker");

#if I2S_SD > 0
  pinMode(I2S_SD, OUTPUT);
  digitalWrite(I2S_SD, HIGH); // enable amp
#endif

  out = new AudioOutputI2S();
  out->SetPinout(I2S_BCLK, I2S_LRC, I2S_DOUT);
  out->SetGain(0.5); // 0.0-1.0, start low for 3W speaker, raise to 0.8 max

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi connecting");
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print("."); }
  Serial.printf("\nWiFi OK: %s  RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  Serial.printf("Polling %s/api/next\n", SERVER);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] disconnected, reconnecting...");
    WiFi.disconnect();
    WiFi.reconnect();
    delay(1000);
    return;
  }

  if (mp3 && mp3->isRunning()) {
    if (!mp3->loop()) {
      Serial.printf("[done] v%d\n", lastVersion);
      int v = lastVersion;
      stopPlayback();
      reportPlayed(v);
    }
  } else {
    // idle: poll every 1.5s
    if (millis() - lastPoll > 1500) {
      lastPoll = millis();
      checkForNew();
    }
    delay(20);
  }
}
