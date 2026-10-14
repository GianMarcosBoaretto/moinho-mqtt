# Guia de uso e de teste

Passo a passo para rodar o protótipo, usar o dashboard e testar cada requisito da entrega.
Para o porquê das decisões, veja o [README](README.md) e a pasta [docs/](docs/).

## 1. O que você precisa

Escolha **um** dos dois jeitos:

| Jeito | Precisa de | Quando usar |
|---|---|---|
| **A. Docker** | Docker Desktop | é o jeito oficial da entrega; usa o Mosquitto de verdade e roda o teste automatizado |
| **B. Sem Docker (Windows)** | só Python 3.12 | máquina sem Docker; usa um broker MQTT em Python no lugar do Mosquitto |

Todos os comandos abaixo rodam na pasta do projeto (`moinho-mqtt`).

## 2. Como rodar

### Jeito A — Docker

```bash
docker compose up --build
```

Espere aparecer `[MQTT] conectado ao broker` nos logs e abra <http://localhost:8000>.
Para parar: `Ctrl+C`, ou `docker compose down` em outro terminal.

### Jeito B — sem Docker (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1
```

Na primeira vez ele cria o ambiente Python (`.venv`) e instala as dependências, o que
leva cerca de 1 minuto. Depois abre **três janelas** e o navegador no dashboard:

| Janela | O que é | O que aparece nela |
|---|---|---|
| `BROKER MQTT` | o "correio" das mensagens | conexões dos clientes |
| `BACKEND` | o supervisório (recebe, grava, serve o dashboard) | `[RX]` a cada leitura recebida |
| `SIMULADOR` | o nó de medição (faz o papel do ESP32) | `[PROTECAO]`, `[MQTT]`, `[BUFFER]` |

Para parar tudo, feche as três janelas.

## 3. Usando o dashboard

Abra <http://localhost:8000>. A tela atualiza sozinha a cada 1 segundo.

| Parte da tela | O que mostra |
|---|---|
| Topo, à direita | `broker: conectado · nó: online` — situação da comunicação |
| **Corrente** | último valor medido, com "medido às HH:MM:SS" e o atraso de entrega (skew) |
| **Estado do nó** | `OPERANDO` (verde), `DESARMADO` ou `EMERGENCIA` (vermelho), com o motivo |
| **Último alarme** | último desarme/rearme e a hora em que aconteceu |
| **Consistência** | leituras gravadas, duplicadas descartadas, inválidas e lacunas de `seq` |
| **Gráfico** | últimos 3 min: azul = tempo real, laranja = chegou atrasado do buffer, tracejado = limite de 100 A |
| **Eventos** | desarmes, rearmes, emergências e nó online/offline |

Quando a comunicação cai, aparece um **banner vermelho** no topo. Nesse momento a corrente
e o estado ficam **em cinza**, com "VALOR ANTIGO" ao lado, e o gráfico ganha uma faixa
"sem dados".

## 4. Comandando o simulador

O simulador aceita comandos que imitam o que aconteceria na bancada:

| Comando | Imita | O que deve acontecer |
|---|---|---|
| `sobrecorrente` | carga pesada no moinho (~128 A) | em ~0,3 s: **DESARMADO**, motivo sobrecorrente, corrente cai para ~0 |
| `normal` | carga volta ao normal (~72 A) | nada visível: o desarme fica **travado** até rearmar |
| `rearmar` | botão de rearme | volta a **OPERANDO** (se a causa ainda existir, desarma de novo) |
| `emergencia` | apertar a botoeira | **EMERGENCIA**, motor para |
| `liberar` | soltar a botoeira | sai da emergência; se havia desarme, continua desarmado |
| `falha_sensor` | sensor rompido | **DESARMADO**, motivo falha de sensor; a corrente aparece como "falha" |

Como enviar:

- **Jeito A (Docker):**
  ```bash
  ./scripts/comando.sh sobrecorrente
  ```
- **Jeito B (sem Docker):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\comando.ps1 sobrecorrente
  ```

O comando chega ao simulador por um arquivo, **sem passar pelo MQTT**. Por isso
funciona também com o broker fora do ar, que é justamente o teste mais importante.

Para voltar ao estado inicial depois de um teste, envie `normal`, `liberar` e `rearmar`.

## 5. Como ver que está transmitindo

São três lugares, um para cada etapa do caminho:

1. **No broker — a mensagem está passando.** Em outro terminal:
   ```powershell
   .\.venv\Scripts\python scripts\espiar.py
   ```
   Deve aparecer uma mensagem `fabrica/planta1/moinho01/corrente` por segundo, com o
   `seq` subindo de 1 em 1. O status `[retained]` aparece logo ao conectar.
   (No Docker: `docker exec moinho-broker mosquitto_sub -t 'fabrica/#' -v`.)
2. **No backend — foi recebida e gravada.** A janela `BACKEND` (ou
   `docker compose logs -f backend`) mostra:
   ```
   [RX] moinho01 seq=4000000015 77.1 A operando skew=1 ms (tempo real)
   ```
3. **No dashboard — foi usada.** "medido às" avança a cada segundo, "gravadas" sobe e o
   gráfico azul cresce para a direita.

A API também pode ser aberta direto no navegador:
<http://localhost:8000/api/estado> ·
<http://localhost:8000/api/historico> ·
<http://localhost:8000/api/eventos>.

## 6. Roteiro de teste manual

Faça na ordem. Cada teste diz o que fazer e o que tem que aparecer. Se aparecer, passou.

### T1 — Fluxo normal

| Faça | Resultado esperado |
|---|---|
| Suba o sistema e espere 10 s | dashboard com ~72 A, **OPERANDO**, topo "broker: conectado · nó: online", sem banner |
| Rode o `espiar.py` | uma mensagem `/corrente` por segundo, `seq` sequencial |

### T2 — Proteção com rede funcionando

| Faça | Resultado esperado |
|---|---|
| Envie `sobrecorrente` | **DESARMADO**, motivo sobrecorrente; corrente ~0 A; "Desarme" na tabela de eventos; pico acima da linha de 100 A no gráfico |
| Envie `rearmar` (sem enviar `normal` antes) | volta a OPERANDO e **desarma de novo** em ~0,3 s, porque a causa continua |
| Envie `normal` e depois `rearmar` | **OPERANDO**, ~72 A, evento "Rearme" |

### T3 — Emergência e falha de sensor

| Faça | Resultado esperado |
|---|---|
| Envie `emergencia` | **EMERGENCIA**, corrente ~0 |
| Envie `rearmar` | **recusado**: o log do simulador diz "rearme recusado: botoeira pressionada" |
| Envie `liberar` e depois `rearmar` | volta a **OPERANDO** |
| Envie `falha_sensor` | **DESARMADO**, motivo falha de sensor, corrente "falha" |
| Envie `normal` e depois `rearmar` | volta a **OPERANDO** |

### T4 — Queda de comunicação (o teste principal)

| Faça | Resultado esperado |
|---|---|
| **Derrube o broker.** Jeito B: feche a janela `BROKER MQTT`. Jeito A: `docker stop moinho-broker` | log do simulador: "broker inacessivel ... A PROTECAO LOCAL CONTINUA ATUANDO" |
| Espere 6 s | **banner vermelho** "Comunicação perdida há Xs — último dado recebido às HH:MM:SS"; valores **em cinza** com "VALOR ANTIGO"; topo "broker: DESCONECTADO"; faixa "sem dados" no gráfico |
| Com o broker ainda fora, envie `sobrecorrente` | **o simulador desarma mesmo sem rede**. O log mostra `estado=desarmado motivo=sobrecorrente ... (decidido localmente, SEM REDE)`. O dashboard **ainda mostra OPERANDO em cinza**, porque o dado não chegou, e o banner avisa disso |
| Espere 15 s | o log do simulador repete a cada 5 s: "sem broker ha Xs - protecao local ATIVA ... buffer N/300" |

### T5 — Volta da comunicação e reconstrução do histórico

| Faça | Resultado esperado |
|---|---|
| **Religue o broker.** Jeito B: `powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SoBroker`. Jeito A: `docker start moinho-broker` | log do simulador: "conectado ao broker (ficou fora Xs); buffer com N mensagens" e "[BUFFER] flush: N mensagens reenviadas" |
| Olhe o dashboard | banner some; **DESARMADO / sobrecorrente**; o trecho da falha aparece **em laranja** no gráfico; o desarme está nos eventos com a **hora em que aconteceu** (durante a falha), e não com a hora em que chegou; "lacunas de seq: 0" |
| Olhe a janela `BACKEND` | linhas `[RX] ... skew=12000 ms (BUFFER)` e depois `[RECUPERADO] ... lacunas restantes: 0` |
| Envie `normal` e `rearmar` | volta a **OPERANDO** |

### T6 — Duplicados são descartados

O reenvio do buffer normalmente não gera duplicado. Para provar que o backend descarta,
reenvie de propósito uma leitura que já foi gravada:

- **Jeito A:** o `teste_falha.sh` faz isso sozinho na etapa 5 (ver a seção 7).
- **Jeito B:**
  ```powershell
  .\.venv\Scripts\python -c "import json,urllib.request as u,paho.mqtt.publish as p; l=json.load(u.urlopen('http://localhost:8000/api/historico'))[-3]; m=json.dumps(dict(schema=1,device_id='moinho01',**{k:l[k] for k in ('seq','ts_origem','corrente_a','estado','motivo')})); [p.single('fabrica/planta1/moinho01/corrente',m,qos=1) for _ in range(2)]; print('reenviado seq',l['seq'])"
  ```

**Resultado esperado:** a janela `BACKEND` mostra duas linhas
`[DUP] moinho01 seq=... ja gravado -> descartado`, e no dashboard "duplicadas
descartadas" sobe 2. O histórico **não** ganha ponto novo.

### T7 — Buffer cheio (opcional, demora 5 min)

| Faça | Resultado esperado |
|---|---|
| Derrube o broker e espere **mais de 5 min** | log do simulador: "[BUFFER] cheio (300): descartando a mais antiga seq=..." |
| Religue o broker | dashboard mostra "lacunas de seq: 1 (N amostras)": o que passou de 5 min se perdeu, e o sistema **avisa** em vez de esconder |

## 7. Teste automatizado (só com Docker)

```bash
./scripts/teste_falha.sh
```

Ele executa T1, T4, T5 e T6 sozinho, em cerca de 1 min 30 s. Com o dashboard aberto ao
lado, tire um print a cada `>>> PRINT:` que aparecer no terminal. No final ele mostra
`RESULTADO: todas as verificacoes passaram` e salva logs e JSON em `logs/<data-hora>/`.
O que cada print precisa mostrar está em [EVIDENCIAS.md](EVIDENCIAS.md).

## 8. Com o ESP32 de verdade

1. Na Arduino IDE, instale as bibliotecas **MQTT** (Joel Gaehwiler) e **ArduinoJson**.
2. Abra `firmware/moinho01/moinho01.ino` e ajuste `config.h`: nome e senha do Wi-Fi e
   `MQTT_HOST` com o **IP do PC** onde o broker roda (`ipconfig` no Windows).
3. Grave na placa (*ESP32 Dev Module*) e abra o Monitor Serial em 115200.
4. **Pare o simulador** antes: os dois publicam como `moinho01`.
5. Os testes são os mesmos da seção 6, trocando os comandos pela bancada:

| Comando do simulador | Na bancada |
|---|---|
| `sobrecorrente` | girar o potenciômetro acima de ~2/3 do curso |
| `normal` | voltar o potenciômetro |
| `rearmar` | apertar o botão de rearme (GPIO 14) por meio segundo |
| `emergencia` / `liberar` | apertar / soltar a botoeira |
| `falha_sensor` | girar o potenciômetro até o fim do curso (qualquer ponta) |
| derrubar o broker | desligar o broker no PC, ou o roteador Wi-Fi |

No T4, a prova mais forte: com o broker desligado, gire o potenciômetro. **O relé tem que
abrir** e o Monitor Serial mostra "SEM REDE".

> No PC, o Firewall do Windows pode bloquear a porta 1883 para a placa. Se o ESP32 não
> conectar, libere a porta 1883 (TCP) na rede privada.

## 9. Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Dashboard abre mas fica "Nenhum dado recebido do nó ainda" | simulador não está rodando ou não achou o broker | veja a janela `SIMULADOR`; se diz "conectando" sem "conectado", o broker está fora |
| Simulador conecta e desconecta a cada ~3 s | **dois simuladores** abertos com o mesmo `client_id` (o broker derruba o mais antigo) | feche as janelas repetidas e deixe um só |
| `error while attempting to bind on address ... 8000` | já tem um backend rodando na porta 8000 | feche a janela `BACKEND` antiga |
| `rodar-local.ps1 não pode ser carregado ... execução de scripts desabilitada` | política de execução do Windows | rode com `powershell -ExecutionPolicy Bypass -File ...`, como neste guia |
| Banner fica vermelho com o broker no ar | relógio do PC errado, ou o backend não reconectou | confira `broker` no topo do dashboard; se estiver "DESCONECTADO", reinicie o backend |
| Quero recomeçar do zero | — | feche tudo e apague `backend/moinho.db` e a pasta `simulador/estado/`. No Docker: `docker compose down -v` |
