/*
  No de medicao do moinho01 (ESP32) - protecao por sobrecorrente + telemetria MQTT.

  Mesma logica e mesmo contrato de topicos/payload do simulador (simulador/simulador.py).

  Divisao em duas partes que NAO dependem uma da outra:

  1. tarefaProtecao (FreeRTOS, prioridade 3, a cada 100 ms)
     le o sensor, decide e aciona o rele. Nao chama Wi-Fi nem MQTT.
     Tem prioridade maior que o loop(): mesmo que o loop() fique preso
     tentando conectar no broker, a protecao continua rodando no horario.

  2. loop() (prioridade 1) - monitoramento
     recebe as amostras da protecao por uma fila, guarda num buffer
     circular de 300 e publica com QoS 1 quando houver Wi-Fi + broker + NTP.

  Bibliotecas (Gerenciador de Bibliotecas da Arduino IDE):
    - "MQTT" de Joel Gaehwiler (256dpi/arduino-mqtt) >= 2.5  -> publica com QoS 1
    - "ArduinoJson" de Benoit Blanchon >= 7.0
  Placa: ESP32 Dev Module (core esp32 da Espressif 3.x)
*/

#define ARDUINOJSON_USE_LONG_LONG 1   // seq e 64 bits
#include <WiFi.h>
#include <MQTT.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <esp_timer.h>
#include <sys/time.h>
#include <time.h>
#include "config.h"

// ------------------------------------------------------------------ topicos
#define PREFIXO "fabrica/" PLANTA "/" DEVICE_ID
const char* TOPICO_CORRENTE = PREFIXO "/corrente";
const char* TOPICO_STATUS   = PREFIXO "/status";
const char* TOPICO_ALARME   = PREFIXO "/alarme";
const int SCHEMA = 1;

// ------------------------------------------------------------------ tipos
enum Estado : uint8_t { OPERANDO, DESARMADO, EMERGENCIA };
enum Motivo : uint8_t { SEM_MOTIVO, SOBRECORRENTE, FALHA_SENSOR };

const char* NOME_ESTADO[] = {"operando", "desarmado", "emergencia"};
const char* NOME_MOTIVO[] = {nullptr, "sobrecorrente", "falha_sensor"};

struct Amostra {
  uint64_t seq;
  int64_t  t_us;           // relogio monotonico do ESP32 no momento da medicao
  float    corrente;
  bool     corrente_valida;
  Estado   estado;
  Motivo   motivo;
  bool     alarme;         // true = mudou de estado nesta amostra (vai tambem para /alarme)
};

// ------------------------------------------------------------------ globais
QueueHandle_t filaAmostras;                // protecao -> monitoramento
volatile Estado estadoAtual = OPERANDO;    // so leitura no loop() (para o status retained)
volatile Motivo motivoAtual = SEM_MOTIVO;
volatile bool brokerConectado = false;     // escrito so pelo loop(); a protecao so le, para o log
uint32_t numeroBoot = 0;

WiFiClient rede;
MQTTClient mqtt(512);

Amostra buffer[TAMANHO_BUFFER];            // buffer circular do monitoramento
int bufInicio = 0, bufQtd = 0;
uint32_t descartadas = 0;

// =================================================================== PROTECAO
// Nada nesta secao usa rede. E a parte que protege o moinho.

struct Protecao {
  Motivo disparo = SEM_MOTIVO;   // travado ate rearmar
  bool botoeira = false;
  int contSobre = 0, contFalha = 0;

  bool releFechado() { return !botoeira && disparo == SEM_MOTIVO; }

  Estado estado() {
    if (botoeira) return EMERGENCIA;
    if (disparo != SEM_MOTIVO) return DESARMADO;
    return OPERANDO;
  }

  void avaliar(bool valida, float corrente) {
    if (disparo != SEM_MOTIVO) return;
    contFalha = valida ? 0 : contFalha + 1;
    contSobre = (valida && corrente > LIMITE_A) ? contSobre + 1 : 0;
    if (contFalha >= AMOSTRAS_CONFIRMACAO)      disparo = FALHA_SENSOR;   // sem medicao = desarma (fail-safe)
    else if (contSobre >= AMOSTRAS_CONFIRMACAO) disparo = SOBRECORRENTE;
  }

  void rearmar() {
    if (botoeira) { Serial.println("[PROTECAO] rearme recusado: botoeira pressionada"); return; }
    disparo = SEM_MOTIVO;
    contSobre = contFalha = 0;
  }
};

bool lerSensor(float& corrente) {
  uint32_t soma = 0;
  for (int i = 0; i < 8; i++) soma += analogRead(PINO_SENSOR);
  int bruto = soma / 8;
  if (bruto < ADC_MIN_VALIDO || bruto > ADC_MAX_VALIDO) return false;   // fio rompido / curto
  corrente = (bruto - ADC_MIN_VALIDO) * FUNDO_ESCALA_A / (ADC_MAX_VALIDO - ADC_MIN_VALIDO);
  return true;
}

void tarefaProtecao(void*) {
  Protecao p;
  uint32_t contador = 0;
  int contRearme = 0;
  int64_t proximaTelemetria = 0;
  TickType_t ultimoAcordar = xTaskGetTickCount();

  for (;;) {
    Estado estadoAntes = p.estado();
    Motivo motivoAntes = p.disparo;

    // entradas fisicas
    p.botoeira = digitalRead(PINO_BOTOEIRA) == HIGH;   // NF aberto ou fio solto = emergencia
    contRearme = digitalRead(PINO_REARME) == LOW ? contRearme + 1 : 0;
    if (contRearme == 3) p.rearmar();                  // 300 ms pressionado (debounce)

    // 1. mede, decide e atua - sem rede
    float corrente = 0;
    bool valida = lerSensor(corrente);
    p.avaliar(valida, corrente);
    digitalWrite(PINO_RELE, p.releFechado() ? HIGH : LOW);

    bool mudou = p.estado() != estadoAntes || p.disparo != motivoAntes;
    estadoAtual = p.estado();
    motivoAtual = p.disparo;
    if (mudou) {
      Serial.printf("[PROTECAO] estado=%s motivo=%s corrente=%.1f A rele=%s (decidido localmente, %s)\n",
                    NOME_ESTADO[p.estado()], p.disparo ? NOME_MOTIVO[p.disparo] : "-", corrente,
                    p.releFechado() ? "FECHADO" : "ABERTO", brokerConectado ? "online" : "SEM REDE");
    }

    // 2. so entrega a amostra para o monitoramento; nunca espera por ele
    int64_t agora = esp_timer_get_time();
    if (mudou || agora >= proximaTelemetria) {
      proximaTelemetria = agora + PERIODO_TELEMETRIA_MS * 1000LL;
      Amostra a = {(uint64_t)numeroBoot * 1000000000ULL + (++contador), agora, corrente, valida,
                   p.estado(), p.disparo, mudou};
      xQueueSend(filaAmostras, &a, 0);   // timeout 0: fila cheia perde telemetria, nunca atrasa a protecao
    }

    vTaskDelayUntil(&ultimoAcordar, pdMS_TO_TICKS(PERIODO_PROTECAO_MS));
  }
}

// ============================================================== MONITORAMENTO

bool relogioSincronizado() { return time(nullptr) > 1700000000; }   // NTP ja respondeu

// ts_origem: instante da MEDICAO, reconstruido a partir do relogio monotonico.
// Funciona inclusive para amostras feitas antes do NTP responder.
void formatarTsOrigem(int64_t t_us, char* saida, size_t tam) {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  int64_t agoraEpochMs = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
  int64_t epochMs = agoraEpochMs - (esp_timer_get_time() - t_us) / 1000;
  time_t seg = epochMs / 1000;
  struct tm utc;
  gmtime_r(&seg, &utc);
  char base[24];
  strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &utc);
  snprintf(saida, tam, "%s.%03dZ", base, (int)(epochMs % 1000));
}

void montarPayload(const Amostra& a, char* saida, size_t tam) {
  char ts[32];
  formatarTsOrigem(a.t_us, ts, sizeof(ts));
  JsonDocument doc;
  doc["schema"] = SCHEMA;
  doc["device_id"] = DEVICE_ID;
  doc["seq"] = a.seq;
  doc["ts_origem"] = ts;
  if (a.corrente_valida) doc["corrente_a"] = roundf(a.corrente * 10) / 10;
  else doc["corrente_a"] = nullptr;
  doc["estado"] = NOME_ESTADO[a.estado];
  if (a.motivo) doc["motivo"] = NOME_MOTIVO[a.motivo];
  else doc["motivo"] = nullptr;
  serializeJson(doc, saida, tam);
}

void publicarStatus() {
  char ts[32], json[200];
  formatarTsOrigem(esp_timer_get_time(), ts, sizeof(ts));
  JsonDocument doc;
  doc["schema"] = SCHEMA;
  doc["device_id"] = DEVICE_ID;
  doc["online"] = true;
  doc["estado"] = NOME_ESTADO[estadoAtual];
  if (motivoAtual) doc["motivo"] = NOME_MOTIVO[motivoAtual];
  else doc["motivo"] = nullptr;
  if (relogioSincronizado()) doc["ts_origem"] = ts;
  serializeJson(doc, json, sizeof(json));
  mqtt.publish(TOPICO_STATUS, json, true, 1);   // retained
}

void guardarNoBuffer(const Amostra& a) {
  if (bufQtd == TAMANHO_BUFFER) {               // cheio: descarta a mais antiga
    bufInicio = (bufInicio + 1) % TAMANHO_BUFFER;
    bufQtd--;
    descartadas++;
  }
  buffer[(bufInicio + bufQtd) % TAMANHO_BUFFER] = a;
  bufQtd++;
}

// Publica na ordem, sem reordenar. Para no primeiro erro: o resto fica para depois.
void esvaziarBuffer() {
  if (!mqtt.connected() || !relogioSincronizado()) return;
  int enviadas = 0;
  bool reenvio = bufQtd > 1;
  char json[256];
  while (bufQtd > 0 && mqtt.connected()) {
    Amostra& a = buffer[bufInicio];
    montarPayload(a, json, sizeof(json));
    if (!mqtt.publish(TOPICO_CORRENTE, json, false, 1)) break;   // QoS 1: espera o PUBACK
    if (a.alarme) {
      if (!mqtt.publish(TOPICO_ALARME, json, false, 1)) break;   // reenvio duplica /corrente; o backend descarta
      publicarStatus();
    }
    bufInicio = (bufInicio + 1) % TAMANHO_BUFFER;
    bufQtd--;
    enviadas++;
    mqtt.loop();
  }
  if (reenvio && enviadas)
    Serial.printf("[BUFFER] flush: %d mensagens reenviadas com ts_origem/seq originais (restam %d)\n", enviadas, bufQtd);
}

void manterConexao() {
  static uint32_t ultimaTentativa = 0;
  static uint32_t foraDesde = 0;
  static bool estavaConectado = false;

  if (mqtt.connected()) return;
  if (estavaConectado) {
    Serial.println("[MQTT] broker inacessivel -> monitoramento suspenso. "
                   "A PROTECAO LOCAL CONTINUA ATUANDO; amostras vao para o buffer.");
    estavaConectado = false;
    foraDesde = millis();
  }
  if (millis() - ultimaTentativa < 2000) return;
  ultimaTentativa = millis();
  if (WiFi.status() != WL_CONNECTED) return;    // o proprio WiFi reconecta (setAutoReconnect)

  if (mqtt.connect(CLIENT_ID)) {
    Serial.printf("[MQTT] conectado ao broker (ficou fora %lus); buffer com %d amostras para reenviar\n",
                  (millis() - foraDesde) / 1000, bufQtd);
    estavaConectado = true;
    publicarStatus();
  }
}

// ======================================================================= setup

void setup() {
  Serial.begin(115200);
  pinMode(PINO_RELE, OUTPUT);
  digitalWrite(PINO_RELE, LOW);                // comeca desligado ate a primeira leitura
  pinMode(PINO_BOTOEIRA, INPUT_PULLUP);
  pinMode(PINO_REARME, INPUT_PULLUP);
  analogReadResolution(12);

  // seq sobrevive a reinicio: o numero do boot fica gravado na flash (NVS)
  Preferences prefs;
  prefs.begin("moinho", false);
  numeroBoot = prefs.getUInt("boot", 0) + 1;
  prefs.putUInt("boot", numeroBoot);
  prefs.end();
  Serial.printf("[NO] %s boot=%lu limite=%.0f A buffer=%d\n", DEVICE_ID, numeroBoot, LIMITE_A, TAMANHO_BUFFER);

  // a protecao sobe ANTES de qualquer coisa de rede
  filaAmostras = xQueueCreate(20, sizeof(Amostra));
  xTaskCreatePinnedToCore(tarefaProtecao, "protecao", 4096, nullptr, 3, nullptr, 1);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_SENHA);           // nao bloqueia
  configTime(0, 0, NTP_1, NTP_2);              // UTC

  char lwt[100];
  snprintf(lwt, sizeof(lwt), "{\"schema\":%d,\"device_id\":\"%s\",\"online\":false}", SCHEMA, DEVICE_ID);
  mqtt.begin(MQTT_HOST, MQTT_PORTA, rede);
  mqtt.setWill(TOPICO_STATUS, lwt, true, 1);   // LWT retained
  mqtt.setOptions(5, false, 1000);             // keepalive 5 s, sessao persistente, timeout 1 s
}

void loop() {
  Amostra a;
  while (xQueueReceive(filaAmostras, &a, 0) == pdTRUE) guardarNoBuffer(a);

  manterConexao();
  mqtt.loop();
  brokerConectado = mqtt.connected();
  esvaziarBuffer();

  static uint32_t proximoAviso = 0;
  if (!mqtt.connected() && millis() >= proximoAviso) {
    proximoAviso = millis() + 5000;
    Serial.printf("[PROTECAO] sem broker - protecao local ATIVA: estado=%s | buffer %d/%d descartadas=%lu%s\n",
                  NOME_ESTADO[estadoAtual], bufQtd, TAMANHO_BUFFER, descartadas,
                  relogioSincronizado() ? "" : " | aguardando NTP");
  }
  delay(10);
}
