// Copy this file to wifi_config.h and fill in your hotspot details (wifi_config.h is git-ignored).
#define WIFI_SSID "YOUR_HOTSPOT_NAME"
#define WIFI_PASS "YOUR_HOTSPOT_PASSWORD"

// The ESP gets a fixed address on the Windows hotspot network (192.168.137.x) so the Pi/laptop always finds it.
// 192.168.137.1 is the laptop; .50 should be free. NOTE: the ESP32 only joins 2.4 GHz networks, and a Windows
// hotspot takes its band from the laptop's own Wi-Fi connection, so the laptop must be on 2.4 GHz.
#define ESP_IP 192, 168, 137, 50
#define GATEWAY_IP 192, 168, 137, 1
