# Arquitetura do fluxo

## Componentes, protocolos e papéis

```mermaid
flowchart LR
    subgraph NO["Nó de medição — ESP32 (ou simulador Python)"]
        direction TB
        SENS["Sensor de corrente<br/>(potenciômetro na bancada)"]
        PROT["Tarefa de PROTEÇÃO<br/>100 ms · decide localmente"]
        RELE["Relé → contator do moinho"]
        BOT["Botoeira de emergência<br/>(em série com o relé)"]
        BUF["Buffer circular<br/>300 amostras"]
        SENS -->|"ADC"| PROT
        PROT -->|"GPIO"| RELE
        BOT -. "corta a bobina<br/>sem software" .-> RELE
        PROT -->|"fila (não bloqueia)"| BUF
    end

    BROKER[("Broker MQTT<br/>Mosquitto :1883<br/>SERVIDOR")]

    subgraph SUP["Serviço supervisório — FastAPI"]
        direction TB
        SUB["Assinante MQTT<br/>valida · deduplica"]
        API["API HTTP :8000<br/>/api/estado · /historico · /eventos"]
    end

    DB[("SQLite<br/>leituras · eventos · dispositivos")]
    NAV["Navegador do operador<br/>dashboard HTML + painel de tráfego"]
    CEL["Celular (nó celular01)<br/>página /celular · proteção + buffer"]
    PONTE["Ponte HTTP → MQTT<br/>POST /api/gateway/*"]

    BUF -->|"MQTT/TCP · QoS 1 · PUBLISH<br/>cliente → servidor<br/>LWT + status retained"| BROKER
    BROKER -->|"MQTT/TCP · QoS 1 · SUBSCRIBE<br/>servidor → cliente (sessão persistente)"| SUB
    SUB -->|"SQL · INSERT OR IGNORE"| DB
    API -->|"SQL · SELECT ORDER BY ts_origem"| DB
    NAV -->|"HTTP GET a cada 1 s<br/>cliente → servidor"| API
    CEL -->|"HTTP POST · cliente → servidor<br/>resposta = confirmação"| PONTE
    PONTE -->|"MQTT · QoS 1 · PUBLISH"| BROKER
```

## Quem é cliente e quem é servidor em cada enlace

| Enlace | Protocolo | Cliente | Servidor | Observação |
|---|---|---|---|---|
| Nó → broker | MQTT 3.1.1 sobre TCP 1883 | nó (`esp32-moinho01` / `sim-moinho01`) | Mosquitto | o nó só **publica**; abre a conexão de saída |
| Backend ↔ broker | MQTT 3.1.1 sobre TCP 1883 | backend (`supervisorio-planta1`) | Mosquitto | o backend **assina** tudo (o broker empurra as mensagens) e **publica** só pela ponte do celular |
| Backend → banco | SQL (biblioteca embutida) | backend | — (SQLite é um arquivo, não um servidor) | escrita pela thread MQTT, leitura pelas rotas HTTP |
| Navegador → backend | HTTP/1.1 sobre TCP 8000 | navegador | backend (uvicorn) | polling de 1 s |
| Celular → backend (ponte) | HTTP/1.1 sobre TCP 8000 | navegador do celular | backend | o backend republica no MQTT com QoS 1 |
| Sensor/botoeira → relé | elétrico | — | — | **fora da rede**: é o caminho de segurança |

No MQTT, publicador e assinante são **os dois clientes**; o único servidor é o broker.
Por isso o nó e o backend não se conhecem e podem cair e voltar de forma independente.

## Sequência de uma falha de comunicação

```mermaid
sequenceDiagram
    autonumber
    participant N as Nó (proteção + buffer)
    participant B as Broker
    participant S as Supervisório
    participant D as Dashboard

    N->>B: PUBLISH corrente seq=11 (QoS 1)
    B->>S: entrega seq=11
    S->>S: INSERT (device_id, seq)
    D->>S: GET /api/estado → atual
    Note over B: docker stop broker
    N--xB: conexão cai
    S--xB: conexão cai
    Note over N: proteção segue: 100 ms, decide e atua<br/>telemetria vai para o buffer (seq 12…29)
    N->>N: sobrecorrente → DESARMA (local, sem rede)
    D->>S: GET /api/estado
    S-->>D: stale=true, idade 13 s
    Note over D: banner "comunicação perdida há 13s"<br/>valores em cinza
    Note over B: docker start broker
    N->>B: reconecta + status online (retained)
    N->>B: flush seq 12…29 (ts_origem originais)
    B->>S: entrega 12…29
    S->>S: INSERT OR IGNORE (duplicados descartados)
    D->>S: GET /api/historico (ORDER BY ts_origem)
    S-->>D: histórico completo, desarme no horário real
```
