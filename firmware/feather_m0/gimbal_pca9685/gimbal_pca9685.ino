// Gimbal firmware for the Adafruit Feather M0 (Basic Proto) + PCA9685 16-channel servo driver.
// The same text commands as the ESP32 firmware, over the Feather's USB serial port (one command per line, one reply per command):
//
//   P<0-180>   pan servo        (PCA9685 channel 0)
//   T<0-180>   tilt servo       (channel 1)
//   L<0-180>   lift / platform  (channel 2)
//   A<0-180>   fourth servo, "aux" (channel 3)
//   C          centre: pan 90, tilt 22, lift 90 (energises those three; aux is NOT touched, it moves only on its own A command)
//   S          status only
//   O          release: every servo goes limp (no pulses)
//   X          fire indicator: the board LED and FIRE_PIN (D12) go high for 250 ms. Moves nothing; wire an LED (with a resistor) to D12 if wanted
//   F<kHz>     trim the PCA9685's oscillator, 20000-30000 (default 25000), if the angles read a few degrees off
//   R          distance in mm: always "R-1" here (no sensor wired for now)      I   sensor model: "I none"
//
// Reply: "P<pan> T<tilt> L<lift> A<aux>" (the angles last commanded; the starting values until the first command), or "ERR ...".
//
// Safety by design:
//   * Every servo stays LIMP (no pulses at all) until the first command for that servo. Nothing moves at power-up, and the
//     firmware never decides to move a servo on its own.
//   * There are NO angle limits in the firmware on purpose: the usable range depends on how each arm is mounted, so the limits
//     live in the laptop software (laptop/tilt_calibration.json) and changing them never needs a flash.
//   * The angle-to-pulse mapping is the same as the ESP32 firmware: 500 us at 0 degrees to 2400 us at 180 degrees.
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

const uint8_t PCA_ADDRESS = 0x40;
const uint8_t CH_PAN = 0, CH_TILT = 1, CH_LIFT = 2, CH_AUX = 3;
const int PULSE_MIN_US = 500, PULSE_MAX_US = 2400;       // same as ESP32Servo attach(pin, 500, 2400)
const int HOME_PAN = 90, HOME_TILT = 22, HOME_LIFT = 90, HOME_AUX = 90;
const int AXES = 4;
const uint8_t FIRE_PIN = 12;                              // a pin you can wire an external LED to; the board LED shows the same pulse
const uint32_t FIRE_PULSE_MS = 250;
uint32_t fireUntil = 0;                                   // millis() when the fire pulse ends (0 = no pulse)
const uint32_t OSC_DEFAULT_HZ = 25000000;                // the PCA9685's internal oscillator, nominally 25 MHz

Adafruit_PWMServoDriver pwm(PCA_ADDRESS);
int angle[AXES] = {HOME_PAN, HOME_TILT, HOME_LIFT, HOME_AUX};   // last commanded (or the starting values)
bool energised[AXES] = {false, false, false, false};
const uint8_t channelOf[AXES] = {CH_PAN, CH_TILT, CH_LIFT, CH_AUX};
const char axisName[AXES] = {'P', 'T', 'L', 'A'};

char buf[16];
size_t used = 0;
bool overflow = false;

void setPulse(int axis) {
  long us = PULSE_MIN_US + (long)angle[axis] * (PULSE_MAX_US - PULSE_MIN_US) / 180;
  pwm.writeMicroseconds(channelOf[axis], us);
  energised[axis] = true;
}

void releaseAll() {
  for (int i = 0; i < AXES; i++) {
    pwm.setPWM(channelOf[i], 0, 4096);                    // "full off": no pulses, the servo goes limp
    energised[i] = false;
  }
}

String status() {
  return "P" + String(angle[0]) + " T" + String(angle[1]) + " L" + String(angle[2]) + " A" + String(angle[3]);
}

String run(const char *cmd) {
  size_t n = strlen(cmd);
  if (strcmp(cmd, "C") == 0) {
    angle[0] = HOME_PAN; angle[1] = HOME_TILT; angle[2] = HOME_LIFT;
    for (int i = 0; i < 3; i++) setPulse(i);               // pan, tilt, lift only: the aux servo moves only when told to
  } else if (strcmp(cmd, "S") == 0) {
    // status only
  } else if (strcmp(cmd, "O") == 0) {
    releaseAll();
  } else if (strcmp(cmd, "X") == 0) {
    digitalWrite(FIRE_PIN, HIGH);
    digitalWrite(LED_BUILTIN, HIGH);
    fireUntil = millis() + FIRE_PULSE_MS;
    if (fireUntil == 0) fireUntil = 1;
  } else if (strcmp(cmd, "R") == 0) {
    return "R-1";
  } else if (strcmp(cmd, "I") == 0) {
    return "I none";
  } else if ((cmd[0] == 'P' || cmd[0] == 'T' || cmd[0] == 'L' || cmd[0] == 'A') && n > 1) {
    if (n > 4) return "ERR invalid angle";
    for (size_t i = 1; i < n; i++)
      if (cmd[i] < '0' || cmd[i] > '9') return "ERR invalid angle";
    int axis = cmd[0] == 'P' ? 0 : cmd[0] == 'T' ? 1 : cmd[0] == 'L' ? 2 : 3;
    angle[axis] = constrain(atoi(cmd + 1), 0, 180);         // 0-180 is the most a servo can be told, nothing narrower here
    setPulse(axis);
  } else if (cmd[0] == 'F' && n > 1 && n <= 6) {
    for (size_t i = 1; i < n; i++)
      if (cmd[i] < '0' || cmd[i] > '9') return "ERR invalid frequency";
    long khz = atol(cmd + 1);
    if (khz < 20000 || khz > 30000) return "ERR frequency out of range";
    pwm.setOscillatorFrequency((uint32_t)khz * 1000UL);
    pwm.setPWMFreq(50);
    for (int i = 0; i < AXES; i++) if (energised[i]) setPulse(i);   // re-issue the pulses at the new calibration
  } else {
    return "ERR unknown command";
  }
  return status();
}

void setup() {
  Serial.begin(115200);              // never waits for a computer: the firmware runs on its own
  pinMode(LED_BUILTIN, OUTPUT);
  pinMode(FIRE_PIN, OUTPUT);
  digitalWrite(FIRE_PIN, LOW);
  Wire.begin();
  Wire.setClock(400000);
  pwm.begin();
  pwm.setOscillatorFrequency(OSC_DEFAULT_HZ);
  pwm.setPWMFreq(50);
  releaseAll();                      // limp at power-up, whatever the PCA9685 was doing before
}

void loop() {
  if (fireUntil && (int32_t)(millis() - fireUntil) >= 0) {   // the fire pulse is over
    digitalWrite(FIRE_PIN, LOW);
    digitalWrite(LED_BUILTIN, LOW);
    fireUntil = 0;
  }
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (overflow) Serial.println("ERR command too long");
      else if (used) {
        buf[used] = '\0';
        Serial.println(run(buf));
      }
      used = 0;
      overflow = false;
      digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));   // the board's LED blinks with each command
    } else if (used < sizeof(buf) - 1) {
      buf[used++] = c;
    } else {
      overflow = true;
    }
  }
}
