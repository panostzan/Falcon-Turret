// Wi-Fi version of gimbal.ino: same pins, same commands, same replies; commands arrive as UDP packets
// (port 4210) as well as over USB serial. Replies go back to whoever sent the command.
//
// Commands (one per packet / line):  P<0-180> pan   T<0-45> tilt   C home   S status   D release (servos go limp)
//                                    A resume (re-attach and hold the last angles)
// Reply: "P<pan> T<tilt>" or "ERR ...". Any P/T/C command re-attaches released servos.
// SAFETY: the tilt bracket only allows 0-45 degrees, so the firmware itself refuses to go outside that,
// whatever the host sends (angles are clamped, never passed through).
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESP32Servo.h>
#include "wifi_config.h"

const uint16_t UDP_PORT = 4210;
const int PAN_PIN = 19, TILT_PIN = 18;
const int PAN_MIN = 0, PAN_MAX = 180, TILT_MIN = 0, TILT_MAX = 45;   // physical travel of this gimbal
const int PAN_HOME = 90, TILT_HOME = 22;                              // start position (middle of each range)

Servo panServo, tiltServo;
int panAngle = PAN_HOME, tiltAngle = TILT_HOME;
bool attached = false;
WiFiUDP udp;
char serialBuf[16];
size_t serialUsed = 0;
bool serialOverflow = false;
unsigned long lastWifiTry = 0;

// Same pulse mapping as Servo.write(angle) for attach(pin, 500, 2400), but sent straight to the timer.
void moveServo(Servo &servo, int angle) {
  servo.writeMicroseconds(500 + (long)angle * (2400 - 500) / 180);
}

void attachServos() {
  if (attached) return;
  panServo.setPeriodHertz(50);
  tiltServo.setPeriodHertz(50);
  panServo.attach(PAN_PIN, 500, 2400);
  tiltServo.attach(TILT_PIN, 500, 2400);
  attached = true;
  moveServo(panServo, panAngle);
  moveServo(tiltServo, tiltAngle);
}

void releaseServos() {   // stop sending the position signal: the servos go limp
  panServo.detach();
  tiltServo.detach();
  attached = false;
}

String status() {
  return "P" + String(panAngle) + " T" + String(tiltAngle);
}

// Runs one command and returns the reply text.
String runCommand(const char *cmd) {
  size_t n = strlen(cmd);
  if (strcmp(cmd, "C") == 0) {
    panAngle = PAN_HOME;
    tiltAngle = TILT_HOME;
    attachServos();
    moveServo(panServo, panAngle);
    moveServo(tiltServo, tiltAngle);
  } else if (strcmp(cmd, "S") == 0) {
    // status only; servos give no position feedback
  } else if (strcmp(cmd, "D") == 0) {
    releaseServos();
  } else if (strcmp(cmd, "A") == 0) {
    attachServos();
  } else if ((cmd[0] == 'P' || cmd[0] == 'T') && n > 1) {
    if (n > 4) return "ERR invalid angle";
    for (size_t i = 1; i < n; ++i)
      if (cmd[i] < '0' || cmd[i] > '9') return "ERR invalid angle";
    int angle = atoi(cmd + 1);
    attachServos();
    if (cmd[0] == 'P') {
      panAngle = constrain(angle, PAN_MIN, PAN_MAX);
      moveServo(panServo, panAngle);
    } else {
      tiltAngle = constrain(angle, TILT_MIN, TILT_MAX);
      moveServo(tiltServo, tiltAngle);
    }
  } else {
    return "ERR unknown command";
  }
  return status();
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency
  WiFi.config(IPAddress(ESP_IP), IPAddress(GATEWAY_IP), IPAddress(255, 255, 255, 0), IPAddress(GATEWAY_IP));
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void setup() {
  Serial.begin(115200);
  attachServos();
  connectWifi();
  udp.begin(UDP_PORT);
  Serial.println(status());
}

void handleUdp() {
  int size = udp.parsePacket();
  if (size <= 0) return;
  char buf[16];
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = '\0';
  while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) buf[--len] = '\0';
  String reply = len > 0 && size < (int)sizeof(buf) ? runCommand(buf) : String("ERR command too long");
  udp.beginPacket(udp.remoteIP(), udp.remotePort());
  udp.print(reply);
  udp.endPacket();
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (serialOverflow) Serial.println("ERR command too long");
      else if (serialUsed) {
        serialBuf[serialUsed] = '\0';
        Serial.println(runCommand(serialBuf));
      }
      serialUsed = 0;
      serialOverflow = false;
    } else if (serialUsed < sizeof(serialBuf) - 1) {
      serialBuf[serialUsed++] = c;
    } else {
      serialOverflow = true;
    }
  }
}

void loop() {
  static bool wasConnected = false;
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected != wasConnected) {  // report join/leave on the USB serial monitor
    wasConnected = connected;
    Serial.println(connected ? "WiFi connected, IP " + WiFi.localIP().toString() : "WiFi disconnected");
  }
  if (!connected && millis() - lastWifiTry > 5000) {  // rejoin if the hotspot drops
    lastWifiTry = millis();
    WiFi.disconnect();
    connectWifi();
  }
  handleUdp();
  handleSerial();
}
