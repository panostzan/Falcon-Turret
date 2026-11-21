// ToF wiring probe: scans the I2C bus and says which VL53 sensor (if any) it finds. READ ONLY: it never touches
// the servos (it does not even attach them) and sends nothing but I2C reads.
//
// Wiring: SDA -> GPIO 23, SCL -> GPIO 22, VIN -> 3V3, GND -> GND. Leave XSHUT / GPIO1 unconnected.
// Open the Serial Monitor at 115200 baud (or let the laptop read COM7); it reports every 2 seconds.
#include <Arduino.h>
#include <Wire.h>

const int SDA_PIN = 23, SCL_PIN = 22;
const uint8_t TOF_ADDR = 0x29;   // default address of both the VL53L0X and the VL53L1X

// VL53L1X: 16-bit register 0x010F holds 0xEA, 0x0110 holds 0xCC.  VL53L0X: 8-bit register 0xC0 holds 0xEE.
bool readL1XId(uint8_t &hi, uint8_t &lo) {
  Wire.beginTransmission(TOF_ADDR);
  Wire.write(0x01);
  Wire.write(0x0F);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)TOF_ADDR, 2) != 2) return false;
  hi = Wire.read();
  lo = Wire.read();
  return true;
}

bool readL0XId(uint8_t &id) {
  Wire.beginTransmission(TOF_ADDR);
  Wire.write(0xC0);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)TOF_ADDR, 1) != 1) return false;
  id = Wire.read();
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(600);
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  Serial.println("\n=== tof_probe: SDA=GPIO23 SCL=GPIO22 ===");
}

void loop() {
  int found = 0;
  Serial.print("I2C devices:");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      found++;
    }
  }
  if (!found) Serial.print(" none");
  Serial.println();

  uint8_t hi = 0, lo = 0, id = 0;
  if (found == 0) {
    Serial.println("RESULT: nothing answered. Check power (3V3), GND, and that SDA/SCL are on GPIO 23/22 (not swapped, not loose).");
  } else if (readL1XId(hi, lo) && hi == 0xEA && lo == 0xCC) {
    Serial.println("RESULT: VL53L1X found at 0x29. Wiring is good.");
  } else if (readL0XId(id) && id == 0xEE) {
    Serial.println("RESULT: VL53L0X found at 0x29. Wiring is good.");
  } else {
    Serial.println("RESULT: something answered but it is not a VL53L0X/VL53L1X at 0x29 (see the address list above).");
  }
  delay(2000);
}
