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

// Bancada: potenciometro simula o sensor de corrente.
// 2% a 98% do curso = 0 a 150 A. Encostar nas pontas simula sensor rompido/em curto.
#define FUNDO_ESCALA_A  150.0f
#define ADC_MIN_VALIDO  82     // ~2% de 4095
#define ADC_MAX_VALIDO  4013   // ~98% de 4095

// Pinos
#define PINO_SENSOR    34   // ADC1 (o ADC2 nao funciona com Wi-Fi ligado)
#define PINO_RELE      26   // HIGH = bobina energizada = motor liga
#define PINO_BOTOEIRA  27   // contato AUXILIAR NF da botoeira para GND (o contato principal fica em serie com o rele)
#define PINO_REARME    14   // botao NA para GND
