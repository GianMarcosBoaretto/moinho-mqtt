# Convenção de nomeação

Todo dado do sistema é identificado da mesma forma em três lugares — tópico MQTT,
payload e banco — para que nunca haja dúvida sobre **de quem** é uma leitura.

## Identificadores

| Nome | Valor no protótipo | Onde aparece | Regra |
|---|---|---|---|
| `planta` | `planta1` | tópico | uma por unidade fabril |
| `device_id` | `moinho01` | tópico, payload, chave do banco | um por **equipamento monitorado**; nunca muda, mesmo trocando o ESP32 |
| `client_id` MQTT | `esp32-moinho01` (placa) · `sim-moinho01` (simulador) · `supervisorio-planta1` (backend) | conexão MQTT | único e estável por **conexão**; dois clientes com o mesmo id derrubam um ao outro |

O celular (`device_id` = `celular01`) não abre conexão MQTT própria: ele envia por HTTP
para `POST /api/gateway/<tipo>`, e o backend publica no tópico dele com a conexão
`supervisorio-planta1`. O `device_id` passa por validação (`[a-z0-9_-]`, até 32
caracteres) antes de virar tópico, para que ninguém publique em `#` ou em outro ramo.

O `device_id` identifica o **moinho**; o `client_id` identifica **quem está conectado**.
Por isso placa e simulador têm `client_id` diferentes, mas publicam com o mesmo
`device_id`: o simulador faz o papel da placa. Os dois nunca devem rodar ao mesmo tempo,
porque misturariam duas sequências de `seq` no mesmo `device_id`.

> Visto na prática durante o desenvolvimento: dois simuladores esquecidos rodando com o
> mesmo `client_id` derrubavam um ao outro a cada ~3 s (o broker desconecta a sessão
> antiga quando chega uma nova com o mesmo id).

## Tópicos

Hierarquia: `fabrica/<planta>/<device_id>/<tipo>`

| Tópico | Conteúdo | QoS | Retained | Quando |
|---|---|---|---|---|
| `fabrica/planta1/moinho01/corrente` | leitura (payload completo) | 1 | não | a cada 1 s e em toda mudança de estado |
| `fabrica/planta1/moinho01/alarme` | a leitura que causou a mudança de estado | 1 | não | desarme, emergência, rearme |
| `fabrica/planta1/moinho01/status` | conectividade + estado atual | 1 | **sim** | ao conectar, a cada mudança, e LWT `online:false` |

O backend assina com curinga de um nível (`fabrica/+/+/corrente` etc.), então um
`moinho02` entra no sistema sem mudar o backend. Ele confere que o `device_id` do
payload é igual ao do tópico e descarta a mensagem se não for.

## Payload de leitura (`/corrente` e `/alarme`) — schema 1

```json
{
  "schema": 1,
  "device_id": "moinho01",
  "seq": 1000000123,
  "ts_origem": "2026-10-13T12:29:22.820Z",
  "corrente_a": 72.4,
  "estado": "operando",
  "motivo": null
}
```

| Campo | Tipo | Regra |
|---|---|---|
| `schema` | inteiro | versão do contrato. O backend só aceita `1`; mudança incompatível = `2` |
| `device_id` | texto | igual ao do tópico |
| `seq` | inteiro 64 bits | monotônico por dispositivo; ver abaixo |
| `ts_origem` | texto ISO-8601 UTC com ms | instante da **medição**, no relógio do nó |
| `corrente_a` | número ou `null` | ampères, 1 casa; `null` = leitura inválida (falha de sensor) |
| `estado` | `operando` \| `desarmado` \| `emergencia` | estado da proteção local |
| `motivo` | `null` \| `sobrecorrente` \| `falha_sensor` | por que desarmou (fica travado até o rearme) |

A mensagem em `/alarme` é **a mesma** leitura publicada em `/corrente`, com o mesmo
`seq`. Assim o alarme não "gasta" um número da sequência e a detecção de lacunas olha
uma sequência só.

## Payload de status (`/status`, retained)

```json
{"schema": 1, "device_id": "moinho01", "online": true,
 "estado": "desarmado", "motivo": "sobrecorrente", "ts_origem": "2026-10-13T12:29:22.820Z"}
```

LWT (publicado pelo broker quando o nó some sem se despedir):

```json
{"schema": 1, "device_id": "moinho01", "online": false}
```

## `seq` e reinício do dispositivo

`seq = boot × 1.000.000.000 + contador`

O número do boot fica gravado (NVS no ESP32, `estado/boot.json` no simulador) e sobe a
cada reinício. Sem isso, o nó voltaria a publicar `seq = 1` depois de um reset e o backend
descartaria todos os dados novos como "duplicados" pela chave `(device_id, seq)`.
Um salto de bloco (boot 1 → boot 2) **não** é contado como lacuna.

## Banco (SQLite)

| Tabela | Chave de unicidade | Conteúdo |
|---|---|---|
| `leituras` | `UNIQUE(device_id, seq)` | uma linha por leitura, com `ts_origem`, `ts_recebimento` e `skew_ms` |
| `eventos` | `UNIQUE(device_id, seq)` | desarme/emergência/rearme (com `seq`) e online/offline (sem `seq`) |
| `dispositivos` | `device_id` | último status de conectividade |
