# Monitoramento e proteção por sobrecorrente de um moinho industrial

Projeto Integrador · Automação Industrial · Entrega: **Comunicação e Consistência**

Grupo: Fernando Grahl Zuffo, Gian Marcos Boaretto, Felipe Castilhos e Luiz Felipe Kauê da Silva.

## O que esta entrega mostra

Um fluxo de dados funcionando de ponta a ponta:

```
nó de medição (ESP32 ou simulador)  →  MQTT (Mosquitto)  →  backend (FastAPI)  →  SQLite  →  dashboard
        origem                          comunicação         recebe e valida      persiste     ação: o operador vê
```

O foco é o comportamento **com falha de comunicação**: derrubamos o broker, o moinho
continua protegido (o desarme é local), a tela avisa que o dado está velho e, quando a
comunicação volta, o histórico é reconstruído sem duplicados nem buracos.

> **Decisão central:** a proteção NÃO depende da rede. O nó mede, decide e aciona o relé
> sozinho. Wi-Fi, MQTT, backend e banco são só a camada de monitoramento.

## Como rodar

Pré-requisito: Docker com Compose.

```bash
docker compose up --build
```

- Dashboard: <http://localhost:8085>
- Broker MQTT: `localhost:1883`

O simulador começa a publicar ~72 A com o moinho `OPERANDO`. Para forçar situações:

```bash
./scripts/comando.sh sobrecorrente
```

```bash
./scripts/comando.sh normal
```

```bash
./scripts/comando.sh rearmar
```

Comandos: `normal`, `sobrecorrente`, `falha_sensor`, `emergencia`, `liberar` (solta a
botoeira), `rearmar`. Eles chegam ao simulador por arquivo, sem passar pelo MQTT,
justamente para funcionarem com o broker fora.

Para acompanhar os logs:

```bash
docker compose logs -f simulador backend
```

### Celular como nó e painel de tráfego

Com o celular no mesmo Wi-Fi, abra `http://IP-DO-PC:8085/celular`. O endereço aparece no
topo do dashboard. O celular passa a **ser o moinho01**, no lugar do simulador (suba com
`rodar-local.ps1 -SemSimulador`): envia leituras por HTTP, o backend
publica no MQTT e o dashboard mostra, no painel **Tráfego**, cada requisição e mensagem
(estilo Wireshark). Detalhes no [GUIA.md](GUIA.md), seções 10 e 11.

### Teste de falha automatizado

```bash
./scripts/teste_falha.sh
```

Sobe tudo, publica, derruba o broker, força uma sobrecorrente sem rede, confere o modo
degradado, sobe o broker, confere o histórico reconstruído e testa a deduplicação. Salva
logs e JSON em `logs/<data-hora>/` e avisa no terminal o momento de cada print
(linhas `>>> PRINT:`). Ver [EVIDENCIAS.md](EVIDENCIAS.md).

### Sem Docker (opcional)

Com um Mosquitto local na porta 1883 e Python 3.12:

```bash
python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt
```

```bash
cd backend && ../.venv/Scripts/python -m uvicorn app:app --port 8085
```

```bash
cd simulador && ../.venv/Scripts/python simulador.py
```

(Em Linux/macOS troque `.venv/Scripts/` por `.venv/bin/`.)

### Firmware (ESP32)

`firmware/moinho01/` — abrir `moinho01.ino` na Arduino IDE, ajustar `config.h` (Wi-Fi e
IP do broker) e instalar as bibliotecas **MQTT** (Joel Gaehwiler, ≥ 2.5) e **ArduinoJson**
(≥ 7). Placa: *ESP32 Dev Module*. **Não rode o simulador e a placa ao mesmo tempo:** os
dois publicam como `moinho01`.

| Função | Pino | Ligação na bancada |
|---|---|---|
| Sensor (potenciômetro) | GPIO 34 | cursor do potenciômetro entre 3V3 e GND |
| Relé | GPIO 26 | módulo relé; HIGH = motor liga |
| Botoeira (contato auxiliar NF) | GPIO 27 | NF para GND; o contato principal fica em série com a bobina do relé |
| Rearme | GPIO 14 | botão NA para GND |

Girar o potenciômetro acima de ~67% do curso (> 100 A) desarma. Encostar nas pontas
(< 2% ou > 98%) simula sensor rompido → `falha_sensor`.

## Estrutura

```
docker-compose.yml        broker + backend + simulador
mosquitto/                configuração do broker (persistência ligada)
simulador/simulador.py    nó de medição simulado (proteção + telemetria + buffer)
firmware/moinho01/        sketch do ESP32, mesma lógica e mesmo contrato
backend/app.py            assinante MQTT, SQLite, API HTTP
backend/static/index.html dashboard (HTML + JS puro, polling 1 s) + painel de tráfego
backend/static/celular.html página do celular como nó transmissor
GUIA.md                   guia de uso e roteiro de teste
scripts/teste_falha.sh    teste de falha de ponta a ponta
scripts/comando.sh        envia comando ao simulador
docs/                     nomeação, sincronização, falha, decisões, arquitetura
EVIDENCIAS.md             roteiro de evidências e onde tirar os prints
```

## Arquitetura do fluxo

Diagrama completo, com protocolos e papéis de cliente/servidor, em
[docs/arquitetura.md](docs/arquitetura.md).

| Etapa | Componente | O que faz |
|---|---|---|
| Origem | simulador / ESP32 | a cada 100 ms mede e decide; a cada 1 s (ou na mudança de estado) gera uma leitura com `seq` e `ts_origem` |
| Comunicação | Mosquitto | `fabrica/planta1/moinho01/{corrente,alarme,status}`, QoS 1, sessão persistente, LWT, status retained |
| Recebe | backend | valida o schema, grava `ts_recebimento` e `skew`, descarta duplicados por `(device_id, seq)` |
| Persiste | SQLite | tabelas `leituras`, `eventos`, `dispositivos` |
| Ação | dashboard | corrente atual, estado, último alarme, histórico por `ts_origem`, banner de comunicação perdida |

## Decisões principais

Detalhes e justificativas em [docs/DECISOES.md](docs/DECISOES.md).

1. **Proteção local:** o desarme não passa pela rede. Botoeira em série com o relé.
   Sensor inválido também desarma (fail-safe).
2. **MQTT com QoS 1 + deduplicação por `(device_id, seq)`:** "pelo menos uma vez" com
   idempotência no banco dá "efetivamente uma vez", sem o custo do QoS 2.
3. **Consistência eventual:** o banco pode ficar atrasado durante a falha e converge
   depois. Nenhuma decisão de segurança lê o banco, e a tela avisa quando o dado é antigo.
4. **Tempo de origem manda:** tudo é ordenado por `ts_origem`; STALE quando
   `agora − ts_origem > 5 s`.

## Documentos

| Documento | Responde |
|---|---|
| [docs/NOMEACAO.md](docs/NOMEACAO.md) | tópicos, `client_id`, `device_id`, schema do payload, `seq` |
| [docs/SINCRONIZACAO.md](docs/SINCRONIZACAO.md) | `ts_origem` × `ts_recebimento`, skew, ordenação, lacunas |
| [docs/FALHA_COMUNICACAO.md](docs/FALHA_COMUNICACAO.md) | o que acontece com os dados na falha, o que o usuário vê, o que acontece na volta, como se detecta dado velho |
| [docs/DECISOES.md](docs/DECISOES.md) | por que MQTT, por que proteção local, por que consistência eventual |
| [docs/arquitetura.md](docs/arquitetura.md) | diagrama Mermaid do fluxo e da sequência de falha |
| [EVIDENCIAS.md](EVIDENCIAS.md) | roteiro de teste, logs esperados e prints |
