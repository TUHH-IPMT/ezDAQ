// serial_trigger_test.ino
//
// Simuliert den seriellen (USB-)Mess-Trigger von ezDAQ (siehe
// gui/serial_trigger.py) ueber eine einfache Pin-Ueberbrueckung am
// Arduino Uno - kein externes Bauteil noetig.
//
// Verkabelung:
//   Pin 2  --- Jumperkabel ---  GND
//   (Pin 2 ist per INPUT_PULLUP intern auf 5V gezogen; die Bruecke zieht
//   ihn auf GND/LOW - das loest genau EIN Trigger-Signal aus.)
//
// Passende Einstellungen im ezDAQ-Trigger-Dialog:
//   COM-Port:         COM3   (aktuell erkannter Port dieses Arduino)
//   Baudrate:         9600   (Standardwert im Dialog, hier bewusst nicht
//                              geaendert)
//   Erwartetes Signal: TRIGGER
//
// gui/serial_trigger.py vergleicht die zuletzt empfangenen Bytes exakt
// gegen "TRIGGER" (UTF-8, OHNE Zeilenumbruch) - deshalb sendet dieses
// Sketch bewusst kein "\n"/"\r" mit.

const int TRIGGER_PIN = 2;
const char* TRIGGER_MESSAGE = "TRIGGER";
const unsigned long DEBOUNCE_MS = 30;

bool lastState = HIGH;

void setup() {
  Serial.begin(9600);
  pinMode(TRIGGER_PIN, INPUT_PULLUP);
}

void loop() {
  bool currentState = digitalRead(TRIGGER_PIN);

  // Nur bei fallender Flanke (nicht ueberbrueckt -> ueberbrueckt) senden,
  // nicht dauerhaft waehrend die Bruecke haelt - so bleibt jede
  // Ueberbrueckung EIN sauberer Trigger, egal wie lange sie gehalten wird.
  if (lastState == HIGH && currentState == LOW) {
    delay(DEBOUNCE_MS);
    if (digitalRead(TRIGGER_PIN) == LOW) {
      Serial.print(TRIGGER_MESSAGE);
    }
  }

  lastState = currentState;
  delay(5);
}
