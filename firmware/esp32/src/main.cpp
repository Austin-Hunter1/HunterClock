// Weasley Clock display node.
//
// One ESP32 and one GC9A01 round display per person. Each node plays a looping
// animation from LittleFS and draws an overlay on top of it: a ring showing how
// far that person is from home on the same log scale as the clock's dial, their
// name, and a status line.
//
// It talks to nothing but MQTT. There is no wire to the Pi, no wire between
// nodes, and no dependency on the motors: a node with a dead WiFi connection
// shows its last known state and says the connection is down, and the hands
// keep working regardless.
//
// UNTESTED ON HARDWARE. No ESP32 or display was connected when this was
// written, and PlatformIO was not available to compile it. See PROGRESS.md.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <DNSServer.h>
#include <JPEGDEC.h>
#include <LittleFS.h>
#include <PubSubClient.h>
#include <WebServer.h>
#include <WiFi.h>

#include "display.h"
#include "loopfile.h"

#ifndef NODE_FIRMWARE_VERSION
#define NODE_FIRMWARE_VERSION "0.1.0"
#endif

// The log distance-to-angle mapping, matching controller/clockd/geo.py and
// birthday_clock.m. The node only needs the fraction, which the controller
// already sends; this is the fallback when an older controller sends only a
// distance.
static const float EARTH_RADIUS_KM = 6371.0f;
static const float MAX_DISTANCE_KM = 3.14159265f * EARTH_RADIUS_KM;

static float distanceFraction(float km) {
  if (km <= 1.0f) return 0.0f;
  if (km >= MAX_DISTANCE_KM) return 1.0f;
  return log10f(km) / log10f(MAX_DISTANCE_KM);
}

// ---------------------------------------------------------------- config

struct Config {
  String person = "unknown";
  String wifiSsid;
  String wifiPassword;
  String mqttHost = "weasley-clock";
  uint16_t mqttPort = 1883;
  String mqttUser;
  String mqttPassword;
  String baseTopic = "clock";
  String loopPath = "/loop.wcl";
  uint8_t brightness = 255;
  bool showName = true;

  String displayTopic() const { return baseTopic + "/display/" + person; }
  String statusTopic() const { return baseTopic + "/node/" + person + "/status"; }
};

static Config config;
static bool configLoaded = false;

static bool loadConfig() {
  File file = LittleFS.open("/config.json", "r");
  if (!file) {
    Serial.println("[config] /config.json not found");
    return false;
  }
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, file);
  file.close();
  if (error) {
    Serial.printf("[config] /config.json is not valid JSON: %s\n", error.c_str());
    return false;
  }
  config.person = doc["person"] | "unknown";
  config.wifiSsid = doc["wifi_ssid"] | "";
  config.wifiPassword = doc["wifi_password"] | "";
  config.mqttHost = doc["mqtt_host"] | "weasley-clock";
  config.mqttPort = doc["mqtt_port"] | 1883;
  config.mqttUser = doc["mqtt_user"] | "";
  config.mqttPassword = doc["mqtt_password"] | "";
  config.baseTopic = doc["base_topic"] | "clock";
  config.loopPath = doc["loop_file"] | "/loop.wcl";
  config.brightness = doc["brightness"] | 255;
  config.showName = doc["show_name"] | true;
  Serial.printf("[config] person=%s mqtt=%s:%u topic=%s\n",
                config.person.c_str(), config.mqttHost.c_str(),
                config.mqttPort, config.displayTopic().c_str());
  return config.wifiSsid.length() > 0;
}

// ------------------------------------------------------------- node state

struct DisplayState {
  String name;
  String status = "unknown";   // home | away | stale | unknown
  String text = "starting up";
  float distanceKm = -1.0f;
  float fraction = 0.0f;
  uint32_t updatedAtMs = 0;    // millis() when this node last heard anything
  bool everReceived = false;
};

static DisplayState state;
static ClockDisplay display;
static LGFX_Sprite canvas(&display);
static LoopFile loopFile;
static JPEGDEC jpeg;

static WiFiClient wifiClient;
static PubSubClient mqtt(wifiClient);

static bool portalActive = false;
static WebServer portal(80);
static DNSServer dns;

static uint16_t frameIndex = 0;
static uint32_t lastFrameMs = 0;
static uint32_t lastStatusMs = 0;
static uint32_t lastMqttAttemptMs = 0;
static bool loopReady = false;

// Colours, matching the UI's palette.
static const uint16_t COL_INK = 0xFFFF;
static const uint16_t COL_DIM = 0x8410;
static const uint16_t COL_BRASS = 0xC4A9;
static const uint16_t COL_WARN = 0xFC00;
static const uint16_t COL_BAD = 0xF800;
static const uint16_t COL_OK = 0x3DA6;

// -------------------------------------------------------- JPEG into sprite

// JPEGDEC hands back decoded blocks; copy each into the sprite.
static int drawMCU(JPEGDRAW *draw) {
  canvas.pushImage(draw->x, draw->y, draw->iWidth, draw->iHeight, draw->pPixels);
  return 1;
}

static void drawFrameToCanvas(const uint8_t *data, uint32_t length) {
  if (jpeg.openRAM((uint8_t *)data, (int)length, drawMCU)) {
    jpeg.setPixelType(RGB565_BIG_ENDIAN);
    jpeg.decode(0, 0, 0);
    jpeg.close();
  }
}

// ------------------------------------------------------------- the overlay

static uint16_t statusColour() {
  if (state.status == "home") return COL_OK;
  if (state.status == "stale") return COL_WARN;
  if (state.status == "unknown") return COL_DIM;
  return COL_BRASS;
}

// The ring is the same log scale as the clock's dial: a full ring is the
// antipode, a quarter of it is about 24 km. It starts at 12 o'clock and fills
// clockwise, so it reads the same way round as the hand it sits beside.
static void drawRing(float fraction, uint16_t colour) {
  const int cx = DISPLAY_WIDTH / 2;
  const int cy = DISPLAY_HEIGHT / 2;
  const int outer = 118;
  const int inner = 110;

  canvas.drawCircle(cx, cy, outer, COL_DIM);
  canvas.drawCircle(cx, cy, inner, COL_DIM);

  if (fraction <= 0.0f) return;
  if (fraction > 1.0f) fraction = 1.0f;
  const float sweep = fraction * 360.0f;
  // LovyanGFX measures angles from 3 o'clock; -90 puts the start at 12.
  canvas.fillArc(cx, cy, outer, inner, -90.0f, -90.0f + sweep, colour);
}

static String ageText() {
  if (!state.everReceived) return "waiting for the clock";
  const uint32_t seconds = (millis() - state.updatedAtMs) / 1000UL;
  if (seconds < 90) return "updated just now";
  const uint32_t minutes = seconds / 60UL;
  if (minutes < 60) return "updated " + String(minutes) + " min ago";
  const uint32_t hours = minutes / 60UL;
  if (hours < 48) return "updated " + String(hours) + " h ago";
  return "updated " + String(hours / 24UL) + " d ago";
}

static void drawOverlay() {
  const int cx = DISPLAY_WIDTH / 2;

  drawRing(state.fraction, statusColour());

  canvas.setTextDatum(middle_center);

  if (config.showName && state.name.length()) {
    canvas.setTextColor(COL_INK);
    canvas.setTextSize(2);
    canvas.drawString(state.name, cx, 76);
  }

  canvas.setTextColor(statusColour());
  canvas.setTextSize(2);
  canvas.drawString(state.text, cx, DISPLAY_HEIGHT / 2 + 34);

  canvas.setTextColor(COL_DIM);
  canvas.setTextSize(1);
  canvas.drawString(ageText(), cx, DISPLAY_HEIGHT / 2 + 58);

  // A small dot that says whether this node is actually hearing anything.
  // Without it, a node showing a stale position looks identical to one that is
  // working, which is the single most confusing failure on a wall of six.
  uint16_t linkColour = COL_BAD;
  if (WiFi.status() == WL_CONNECTED) linkColour = mqtt.connected() ? COL_OK : COL_WARN;
  canvas.fillCircle(cx, DISPLAY_HEIGHT - 26, 3, linkColour);
}

static void renderOnce() {
  if (loopReady && loopFile.ok()) {
    uint32_t length = 0;
    const uint8_t *data = loopFile.frame(frameIndex, &length);
    if (data) {
      drawFrameToCanvas(data, length);
    } else {
      canvas.fillScreen(TFT_BLACK);
    }
  } else {
    canvas.fillScreen(TFT_BLACK);
    if (loopFile.error()[0]) {
      canvas.setTextDatum(middle_center);
      canvas.setTextColor(COL_DIM);
      canvas.setTextSize(1);
      canvas.drawString(loopFile.error(), DISPLAY_WIDTH / 2, 40);
    }
  }
  drawOverlay();
  canvas.pushSprite(0, 0);
}

// ----------------------------------------------------------------- MQTT

static void onMessage(char *topic, byte *payload, unsigned int length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) {
    Serial.println("[mqtt] payload is not JSON");
    return;
  }
  state.name = doc["name"] | state.name;
  state.status = doc["status"] | "unknown";
  state.text = doc["text"] | "";
  state.distanceKm = doc["distance_km"] | -1.0f;

  if (doc["fraction"].is<float>()) {
    state.fraction = doc["fraction"];
  } else if (state.distanceKm >= 0) {
    state.fraction = distanceFraction(state.distanceKm);
  }
  state.updatedAtMs = millis();
  state.everReceived = true;
  Serial.printf("[mqtt] %s: %s (%.3f)\n", topic, state.text.c_str(), state.fraction);
}

static void publishStatus() {
  if (!mqtt.connected()) return;
  JsonDocument doc;
  doc["person"] = config.person;
  doc["fw"] = NODE_FIRMWARE_VERSION;
  doc["uptime_s"] = millis() / 1000UL;
  doc["rssi"] = WiFi.RSSI();
  doc["ip"] = WiFi.localIP().toString();
  doc["heap"] = ESP.getFreeHeap();
  doc["frames"] = loopFile.count();
  doc["loop_ok"] = loopReady;
  char buffer[256];
  const size_t written = serializeJson(doc, buffer, sizeof(buffer));
  mqtt.publish(config.statusTopic().c_str(), (const uint8_t *)buffer, written, true);
}

static void connectMqtt() {
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (millis() - lastMqttAttemptMs < 5000UL) return;
  lastMqttAttemptMs = millis();

  const String clientId = "weasley-" + config.person + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  Serial.printf("[mqtt] connecting to %s:%u as %s\n",
                config.mqttHost.c_str(), config.mqttPort, clientId.c_str());

  bool connected;
  if (config.mqttUser.length()) {
    connected = mqtt.connect(clientId.c_str(), config.mqttUser.c_str(),
                             config.mqttPassword.c_str());
  } else {
    connected = mqtt.connect(clientId.c_str());
  }
  if (connected) {
    Serial.println("[mqtt] connected");
    // The controller retains clock/display/<person>, so subscribing is enough
    // to get the current state straight away -- no request needed.
    mqtt.subscribe(config.displayTopic().c_str(), 1);
    publishStatus();
  } else {
    Serial.printf("[mqtt] failed, rc=%d\n", mqtt.state());
  }
}

// --------------------------------------------------------- setup portal

// When there is no usable WiFi config, bring up an access point and a one-page
// form. That is the only way to configure a node that is already inside a
// cabinet with no USB access.
static const char PORTAL_PAGE[] PROGMEM = R"HTML(
<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Weasley Clock display node</title>
<style>
body{font:16px system-ui;margin:0;background:#F7F3EA;color:#2B2419;padding:20px}
h1{font-size:20px;margin:0 0 4px}p{color:#7D705D;margin:0 0 18px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#7D705D;margin:12px 0 3px}
input{width:100%;box-sizing:border-box;padding:11px;font-size:16px;border:1px solid #B9A98C;border-radius:8px;background:#fff}
button{margin-top:18px;width:100%;padding:14px;font-size:16px;font-weight:600;background:#A8672B;color:#fff;border:0;border-radius:8px}
</style>
<h1>Display node setup</h1>
<p>This node has no WiFi yet. Fill this in and it will restart and join.</p>
<form method=POST action=/save>
<label>Person (lowercase)</label><input name=person value="%PERSON%" required>
<label>WiFi network</label><input name=ssid required>
<label>WiFi password</label><input name=pass type=password>
<label>MQTT host</label><input name=host value="%HOST%" required>
<label>MQTT username</label><input name=user value="%USER%">
<label>MQTT password</label><input name=mpass type=password>
<button type=submit>Save and restart</button>
</form>
)HTML";

static void startPortal() {
  portalActive = true;
  const String ssid = "WeasleyClock-" + config.person;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(ssid.c_str());
  dns.start(53, "*", WiFi.softAPIP());

  portal.on("/", []() {
    String page = FPSTR(PORTAL_PAGE);
    page.replace("%PERSON%", config.person);
    page.replace("%HOST%", config.mqttHost);
    page.replace("%USER%", config.mqttUser);
    portal.send(200, "text/html", page);
  });
  portal.on("/save", HTTP_POST, []() {
    JsonDocument doc;
    doc["person"] = portal.arg("person");
    doc["wifi_ssid"] = portal.arg("ssid");
    doc["wifi_password"] = portal.arg("pass");
    doc["mqtt_host"] = portal.arg("host");
    doc["mqtt_port"] = config.mqttPort;
    doc["mqtt_user"] = portal.arg("user");
    doc["mqtt_password"] = portal.arg("mpass");
    doc["base_topic"] = config.baseTopic;
    doc["loop_file"] = config.loopPath;
    doc["brightness"] = config.brightness;
    doc["show_name"] = config.showName;

    File file = LittleFS.open("/config.json", "w");
    if (!file) {
      portal.send(500, "text/plain", "Could not write config.json");
      return;
    }
    serializeJsonPretty(doc, file);
    file.close();
    portal.send(200, "text/html",
                "<meta charset=utf-8><p style='font:16px system-ui;padding:20px'>"
                "Saved. Restarting&hellip;</p>");
    delay(800);
    ESP.restart();
  });
  portal.onNotFound([]() {  // captive-portal redirect
    portal.sendHeader("Location", String("http://") + WiFi.softAPIP().toString(), true);
    portal.send(302, "text/plain", "");
  });
  portal.begin();

  Serial.printf("[portal] join WiFi \"%s\" and open http://%s/\n",
                ssid.c_str(), WiFi.softAPIP().toString().c_str());

  canvas.fillScreen(TFT_BLACK);
  canvas.setTextDatum(middle_center);
  canvas.setTextColor(COL_BRASS);
  canvas.setTextSize(2);
  canvas.drawString("Setup", DISPLAY_WIDTH / 2, 92);
  canvas.setTextColor(COL_INK);
  canvas.setTextSize(1);
  canvas.drawString("Join WiFi", DISPLAY_WIDTH / 2, 124);
  canvas.drawString(ssid, DISPLAY_WIDTH / 2, 142);
  canvas.setTextColor(COL_DIM);
  canvas.drawString(WiFi.softAPIP().toString(), DISPLAY_WIDTH / 2, 164);
  canvas.pushSprite(0, 0);
}

// ----------------------------------------------------------------- setup

static void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(("weasley-" + config.person).c_str());
  WiFi.setAutoReconnect(true);
  WiFi.begin(config.wifiSsid.c_str(), config.wifiPassword.c_str());

  Serial.printf("[wifi] joining %s", config.wifiSsid.c_str());
  const uint32_t deadline = millis() + 20000UL;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] %s, RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    // Not fatal: keep showing the last state and let WiFi retry in the
    // background. A node that reboots into a setup portal every time the
    // router restarts would be worse than one that waits.
    Serial.println("[wifi] not connected; will keep retrying");
  }
}

static void startOta() {
  ArduinoOTA.setHostname(("weasley-" + config.person).c_str());
  ArduinoOTA.onStart([]() {
    canvas.fillScreen(TFT_BLACK);
    canvas.setTextDatum(middle_center);
    canvas.setTextColor(COL_BRASS);
    canvas.setTextSize(2);
    canvas.drawString("Updating", DISPLAY_WIDTH / 2, DISPLAY_HEIGHT / 2);
    canvas.pushSprite(0, 0);
  });
  ArduinoOTA.onProgress([](unsigned int done, unsigned int total) {
    canvas.fillRect(40, 150, 160, 6, COL_DIM);
    canvas.fillRect(40, 150, (int)(160.0 * done / total), 6, COL_BRASS);
    canvas.pushSprite(0, 0);
  });
  ArduinoOTA.begin();
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nWeasley Clock display node %s\n", NODE_FIRMWARE_VERSION);

  display.init();
  display.setRotation(0);
  display.setBrightness(config.brightness);
  display.fillScreen(TFT_BLACK);

  canvas.setColorDepth(16);
  if (!canvas.createSprite(DISPLAY_WIDTH, DISPLAY_HEIGHT)) {
    // 240*240*2 is 115 KB; without PSRAM that is most of the heap, so if it
    // fails there is no point pretending. Say so on the panel itself.
    display.setTextDatum(middle_center);
    display.setTextColor(COL_BAD);
    display.drawString("out of memory", DISPLAY_WIDTH / 2, DISPLAY_HEIGHT / 2);
    Serial.println("[display] could not allocate the 240x240 sprite");
    return;
  }

  if (!LittleFS.begin(true)) {
    Serial.println("[fs] LittleFS would not mount");
  }

  configLoaded = loadConfig();
  display.setBrightness(config.brightness);
  state.name = config.person;
  if (state.name.length()) state.name[0] = toupper(state.name[0]);

  loopReady = loopFile.open(LittleFS, config.loopPath.c_str());
  if (loopReady) {
    Serial.printf("[loop] %u frames, %ux%u at %u fps\n", loopFile.count(),
                  loopFile.width(), loopFile.height(), loopFile.fps());
  } else {
    Serial.printf("[loop] %s\n", loopFile.error());
  }

  if (!configLoaded) {
    startPortal();
    return;
  }

  connectWifi();
  startOta();
  mqtt.setServer(config.mqttHost.c_str(), config.mqttPort);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(1024);
  connectMqtt();
}

void loop() {
  if (portalActive) {
    dns.processNextRequest();
    portal.handleClient();
    delay(5);
    return;
  }

  ArduinoOTA.handle();

  if (WiFi.status() == WL_CONNECTED) {
    if (!mqtt.connected()) connectMqtt();
    mqtt.loop();
  }

  const uint32_t now = millis();

  if (now - lastStatusMs > 30000UL) {
    lastStatusMs = now;
    publishStatus();
  }

  const uint32_t interval = loopReady ? loopFile.frameIntervalMs() : 1000UL;
  if (now - lastFrameMs >= interval) {
    lastFrameMs = now;
    if (loopReady && loopFile.count()) {
      frameIndex = (uint16_t)((frameIndex + 1) % loopFile.count());
    }
    renderOnce();
  }

  delay(2);
}
