// IR receiver probe: READ ONLY. Watches the sensor's OUT pin and prints what it sees, so we can tell what kind of
// part it is and how it behaves. It never drives the pin and never touches the servos.
//
// Wiring: sensor VCC -> 3V3, GND -> GND, OUT -> GPIO 27.
// Things to try (watch the Serial Monitor at 115200, or let the laptop read the port):
//   1. leave it alone: it prints the idle level (HIGH or LOW)
//   2. press a button on a TV remote, aimed at the sensor: a 38 kHz receiver prints a "BURST" of edges
//   3. wave a hand close to the sensor, or shine a phone flashlight at it: an obstacle-type module toggles
#include <Arduino.h>

const int IR_PIN = 27;
const uint16_t RING = 256;
volatile uint32_t edgeAt[RING];
volatile uint8_t edgeLevel[RING];
volatile uint16_t head = 0;

void IRAM_ATTR onEdge() {
  uint16_t h = head;
  edgeAt[h % RING] = micros();
  edgeLevel[h % RING] = gpio_get_level((gpio_num_t)IR_PIN);
  head = h + 1;
}

void setup() {
  Serial.begin(115200);
  pinMode(IR_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onEdge, CHANGE);
  delay(300);
  Serial.println("\n=== ir_probe: sensor OUT on GPIO 27 ===");
}

void loop() {
  static uint16_t tail = 0;
  static uint32_t lastEdgeAt = 0, lastReport = 0, totalEdges = 0;
  static uint32_t times[RING];
  static uint8_t levels[RING];
  static uint16_t n = 0;

  while (tail != head) {                          // collect new edges into the current burst
    if (n < RING) {
      times[n] = edgeAt[tail % RING];
      levels[n] = edgeLevel[tail % RING];
      n++;
    }
    lastEdgeAt = edgeAt[tail % RING];
    tail++;
    totalEdges++;
  }

  if (n > 0 && micros() - lastEdgeAt > 40000) {   // 40 ms with no edge: the burst is over, describe it
    Serial.printf("BURST: %u edges, first edge goes %s. widths (us):", n, levels[0] ? "HIGH" : "LOW");
    for (uint16_t i = 1; i < n && i < 48; i++) Serial.printf(" %lu", (unsigned long)(times[i] - times[i - 1]));
    if (n > 48) Serial.print(" ...");
    Serial.println();
    if (n >= 20) Serial.println("  -> many edges: this looks like a coded 38 kHz remote burst (the part decodes remote-style signals)");
    else if (n <= 4) Serial.println("  -> few edges: a plain on/off output (an obstacle- or light-style module)");
    n = 0;
  }

  if (millis() - lastReport > 2000) {
    lastReport = millis();
    Serial.printf("idle level: %s | edges seen so far: %lu\n", digitalRead(IR_PIN) ? "HIGH" : "LOW", (unsigned long)totalEdges);
  }
}
