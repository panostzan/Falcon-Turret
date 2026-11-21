#pragma once
// Optional time-of-flight distance sensor on I2C: SDA = GPIO 23, SCL = GPIO 22. A VL53L0X or a VL53L1X is detected
// automatically (VL53L1X reads up to ~4 m, VL53L0X up to ~1.2 m).
//
// Nothing here ever waits: tofPoll() is called from loop() and takes a reading only when one is ready. If no sensor
// answers, tofDistance() returns -1 and the servo firmware behaves exactly as it does without this file.
#include <Arduino.h>
#include <Wire.h>
#include <VL53L0X.h>
#include <VL53L1X.h>

const int TOF_SDA = 23, TOF_SCL = 22;
const uint8_t TOF_ADDR = 0x29;                 // default address of both sensors
const unsigned long TOF_STALE_MS = 500;        // a reading older than this counts as "no reading"

enum TofModel { TOF_NONE, TOF_L0X, TOF_L1X };
TofModel tofModel = TOF_NONE;
VL53L0X tofL0;
VL53L1X tofL1;
int tofMm = -1;
unsigned long tofAt = 0;

// Reads n bytes starting at a register whose address is regLen bytes long (VL53L1X: 2, VL53L0X: 1).
bool tofReadReg(const uint8_t *reg, size_t regLen, uint8_t *out, size_t n) {
  Wire.beginTransmission(TOF_ADDR);
  Wire.write(reg, regLen);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)TOF_ADDR, (int)n) != (int)n) return false;
  for (size_t i = 0; i < n; i++) out[i] = Wire.read();
  return true;
}

TofModel tofDetect() {
  uint8_t id[2];
  const uint8_t l1x[2] = {0x01, 0x0F};         // VL53L1X: 0x010F -> 0xEA, 0x0110 -> 0xCC
  if (tofReadReg(l1x, 2, id, 2) && id[0] == 0xEA && id[1] == 0xCC) return TOF_L1X;
  const uint8_t l0x[1] = {0xC0};               // VL53L0X: 0xC0 -> 0xEE
  if (tofReadReg(l0x, 1, id, 1) && id[0] == 0xEE) return TOF_L0X;
  return TOF_NONE;
}

void tofBegin() {
  Wire.begin(TOF_SDA, TOF_SCL);
  Wire.setClock(400000);
  Wire.setTimeOut(10);                         // ms: a missing or stuck sensor must never stall the servo firmware
  tofModel = tofDetect();
  if (tofModel == TOF_L1X) {
    tofL1.setBus(&Wire);
    tofL1.setTimeout(20);
    if (tofL1.init()) {
      tofL1.setDistanceMode(VL53L1X::Long);
      tofL1.setMeasurementTimingBudget(33000);
      tofL1.startContinuous(33);
    } else {
      tofModel = TOF_NONE;
    }
  } else if (tofModel == TOF_L0X) {
    tofL0.setBus(&Wire);
    tofL0.setTimeout(20);
    if (tofL0.init()) tofL0.startContinuous();
    else tofModel = TOF_NONE;
  }
}

void tofPoll() {
  if (tofModel == TOF_L1X) {
    if (tofL1.dataReady()) {
      tofL1.read(false);
      tofMm = tofL1.ranging_data.range_status == VL53L1X::RangeValid ? tofL1.ranging_data.range_mm : -1;
      tofAt = millis();
    }
  } else if (tofModel == TOF_L0X) {
    if (tofL0.readReg(VL53L0X::RESULT_INTERRUPT_STATUS) & 0x07) {
      uint16_t mm = tofL0.readRangeContinuousMillimeters();
      tofMm = (tofL0.timeoutOccurred() || mm >= 8190) ? -1 : mm;   // 8190+ means "nothing in range"
      tofAt = millis();
    }
  }
}

// Latest distance in millimetres, or -1 when there is no sensor / no valid or recent reading.
int tofDistance() {
  if (tofModel == TOF_NONE || millis() - tofAt > TOF_STALE_MS) return -1;
  return tofMm;
}

const char *tofName() {
  return tofModel == TOF_L1X ? "VL53L1X" : tofModel == TOF_L0X ? "VL53L0X" : "none";
}
