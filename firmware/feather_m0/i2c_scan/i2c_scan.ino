// First bring-up test for the Feather M0 + PCA9685: scans the I2C bus and prints which addresses answer.
// It does NOT talk to the servos (the PCA9685 is only asked "are you there?"), so nothing moves.
//
// Expected with the servo driver wired to SDA/SCL: 0x40 (the PCA9685) and usually 0x70 (its "all-call" address).
// Open the serial monitor at 115200 (or just read COM port output): it repeats every 2 seconds.
#include <Arduino.h>
#include <Wire.h>

const char *nameOf(uint8_t a) {
  switch (a) {
    case 0x40: return "PCA9685 servo driver";
    case 0x70: return "PCA9685 all-call address (normal)";
    case 0x29: return "VL53 distance sensor";
    default: return "unknown device";
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  Wire.begin();
  Wire.setClock(100000);   // slow and safe for a first test
}

void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  int found = 0;
  Serial.println("I2C scan:");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.print("  0x");
      if (a < 16) Serial.print('0');
      Serial.print(a, HEX);
      Serial.print("  ");
      Serial.println(nameOf(a));
      found++;
    }
  }
  Serial.print(found);
  Serial.println(found == 0 ? " devices found. Check SDA, SCL, GND and the 3V to VCC wire." : " device(s) found.");
  digitalWrite(LED_BUILTIN, LOW);
  delay(2000);
}
