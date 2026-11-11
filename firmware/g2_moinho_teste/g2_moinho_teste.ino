// G2 Moinho: teste da bancada, sem rede
// O potenciômetro simula a corrente do motor. Acima do limite, o relé desarma até o rearme.
// Placa: ESP32 Dev Module
const int PINO_POT     = 34;
const int PINO_RELE    = 26;
const int LED_VERDE    = 19;
const int LED_VERMELHO = 18;
const int PINO_REARME  = 14;
const bool RELE_LIGA   = HIGH;

const float CORRENTE_MAX_A = 150.0;  // escala do potenciômetro
const float LIMITE_A       = 120.0;

bool armado = true;

float lerCorrenteA() {
  return analogRead(PINO_POT) * CORRENTE_MAX_A / 4095.0;
}

void setup() {
  Serial.begin(115200);
  pinMode(PINO_RELE, OUTPUT);
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_VERMELHO, OUTPUT);
  pinMode(PINO_REARME, INPUT_PULLUP);
}

void loop() {
  float i = lerCorrenteA();

  if (armado && i > LIMITE_A) {
    armado = false;
    Serial.printf("DESARME por sobrecorrente: %.2f A\n", i);
  }
  if (!armado && digitalRead(PINO_REARME) == LOW && i <= LIMITE_A) {
    armado = true;
    Serial.println("rearme");
  }

  digitalWrite(PINO_RELE, armado ? RELE_LIGA : !RELE_LIGA);
  digitalWrite(LED_VERDE, armado);
  digitalWrite(LED_VERMELHO, !armado);
  Serial.printf("corrente: %.2f A  estado: %s\n", i, armado ? "operando" : "desarmado");
  delay(100);
}
