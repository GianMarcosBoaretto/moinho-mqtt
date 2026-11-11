// Configuracao do no de medicao moinho01. Ajuste a rede antes de gravar.
// As SENHAS nao ficam aqui: ficam em secrets.h, que esta no .gitignore.
// Copie secrets.exemplo.h para secrets.h e preencha.
#pragma once
#include "secrets.h"   // WIFI_SENHA e MQTT_SENHA

#define WIFI_SSID   "SUA_REDE"

#define MQTT_HOST   "192.168.0.10"   // IP da maquina que roda o docker compose
#define MQTT_PORTA  1883

#define PLANTA      "planta1"
#define DEVICE_ID   "moinho01"
#define CLIENT_ID   "esp32-moinho01"  // unico e estavel; o simulador usa sim-moinho01
#define MQTT_USUARIO "no-moinho01"    // usuario no broker (a senha esta em secrets.h)

#define NTP_1       "a.st1.ntp.br"
#define NTP_2       "pool.ntp.org"

// Protecao (mesmos valores do simulador)
#define LIMITE_A              100.0f  // corrente de desarme
#define AMOSTRAS_CONFIRMACAO  3       // 3 x 100 ms acima do limite = desarme
#define PERIODO_PROTECAO_MS   100
#define PERIODO_TELEMETRIA_MS 1000
#define TAMANHO_BUFFER        300

// Bancada: potenciometro simula o sensor de corrente (mesma escala do g2_moinho_teste).
// Curso todo = 0 a 150 A; o limite de 100 A fica em ~2/3 do curso.
// Encostar no fim do curso (leitura saturada) simula sensor em curto -> falha_sensor.
#define FUNDO_ESCALA_A  150.0f
#define ADC_SATURADO    4090   // leitura media >= isto = fora de escala

// Pinos (iguais ao diagrama de ligacao da aula: G2-moinho-aula13)
#define PINO_SENSOR        34   // ADC1 (o ADC2 nao funciona com Wi-Fi ligado)
#define PINO_RELE          26   // HIGH = bobina energizada = motor liga (rele no NO)
#define PINO_BOTOEIRA      27   // jumper para GND = normal; sem jumper = emergencia (contato auxiliar NF)
#define PINO_REARME        14   // botao NA para GND
#define PINO_LED_VERDE     19   // operando (motor liberado)
#define PINO_LED_VERMELHO  18   // desarmado ou emergencia
