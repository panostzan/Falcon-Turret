// Wi-Fi diagnostic: scans for networks, tries to join the hotspot (DHCP), prints why it fails.
// Does not touch the servos. Uses the hotspot name/password from ../gimbal_wifi/wifi_config.h.
#include <WiFi.h>
#include "../gimbal_wifi/wifi_config.h"

const char *authName(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN: return "open";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA";
    case WIFI_AUTH_WPA2_PSK: return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA/WPA2";
    case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2-ENT";
    case WIFI_AUTH_WPA3_PSK: return "WPA3";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default: return "other";
  }
}

void onEvent(arduino_event_id_t event, arduino_event_info_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED)
    Serial.printf("  event: disconnected, reason code %d\n", info.wifi_sta_disconnected.reason);
  else if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED)
    Serial.println("  event: associated with the access point");
  else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP)
    Serial.printf("  event: got IP %s\n", WiFi.localIP().toString().c_str());
}

void scan() {
  Serial.println("Scanning...");
  int n = WiFi.scanNetworks();
  Serial.printf("%d networks found\n", n);
  for (int i = 0; i < n; i++)
    Serial.printf("  %-24s ch %2d  rssi %4d  %s%s\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                  authName(WiFi.encryptionType(i)), WiFi.SSID(i) == WIFI_SSID ? "   <== our hotspot" : "");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== wifi_diag ===");
  Serial.printf("target SSID \"%s\" (password length %d)\n", WIFI_SSID, (int)strlen(WIFI_PASS));
  WiFi.onEvent(onEvent);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(200);
  scan();
  Serial.println("Joining...");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  static unsigned long last = 0;
  if (millis() - last > 3000) {
    last = millis();
    if (WiFi.status() == WL_CONNECTED)
      Serial.printf("CONNECTED ip %s gw %s rssi %d\n", WiFi.localIP().toString().c_str(),
                    WiFi.gatewayIP().toString().c_str(), WiFi.RSSI());
    else
      Serial.printf("not connected (status %d)\n", WiFi.status());
  }
}
