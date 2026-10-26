"""
Servico supervisorio (backend): assina o MQTT, valida, grava no SQLite e serve o dashboard.

Regras de consistencia:
  - ordenacao/consulta sempre por ts_origem (nunca pela ordem de chegada);
  - UNIQUE(device_id, seq) torna a gravacao idempotente: duplicados (reenvio
    QoS 1, flush do buffer) sao descartados pela chave;
  - o dado e STALE quando (agora - ts_origem) > STALE_S.

Nenhuma decisao de seguranca depende deste servico. Ele so monitora.
"""

import hmac
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
CLIENT_ID = os.getenv("CLIENT_ID", "supervisorio-planta1")
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "moinho.db"))
STALE_S = float(os.getenv("STALE_S", "5"))
# Credenciais vem do ambiente (.env), nunca do codigo. Ver docs/SEGURANCA.md
MQTT_USER = os.getenv("MQTT_USER", "supervisorio")
MQTT_PASS = os.getenv("MQTT_PASS", "")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
LOG_PATH = os.getenv("LOG_PATH", str(Path(__file__).resolve().parent.parent / "logs" / "supervisorio.log"))
SCHEMA_ACEITO = 1
ESTADOS = {"operando", "desarmado", "emergencia"}
MOTIVOS = {None, "sobrecorrente", "falha_sensor"}
BLOCO_BOOT = 1_000_000_000   # seq = boot * BLOCO_BOOT + contador (ver docs/NOMEACAO.md)
PLANTA_GATEWAY = os.getenv("PLANTA_GATEWAY", "planta1")
DEVICE_ID_VALIDO = re.compile(r"^[a-z0-9_-]{1,32}$")   # entra no topico: nada de "/", "#" ou "+"

Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")])
log = logging.getLogger("backend")

# ------------------------------------------------------------------- banco
DDL = """
CREATE TABLE IF NOT EXISTS leituras (
    id             INTEGER PRIMARY KEY,
    device_id      TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    ts_origem      TEXT    NOT NULL,   -- quando foi medido (relogio do no, UTC)
    ts_recebimento TEXT    NOT NULL,   -- quando chegou aqui (relogio do backend, UTC)
    skew_ms        INTEGER NOT NULL,   -- ts_recebimento - ts_origem
    corrente_a     REAL,               -- NULL = falha de sensor
    estado         TEXT    NOT NULL,
    motivo         TEXT,
    UNIQUE (device_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_leituras_ts ON leituras (device_id, ts_origem);

CREATE TABLE IF NOT EXISTS eventos (
    id             INTEGER PRIMARY KEY,
    device_id      TEXT NOT NULL,
    seq            INTEGER,            -- NULL nos eventos de conectividade (online/offline)
    tipo           TEXT NOT NULL,      -- desarme | emergencia | rearme | online | offline
    ts_origem      TEXT,
    ts_recebimento TEXT NOT NULL,
    estado         TEXT,
    motivo         TEXT,
    corrente_a     REAL,
    detalhe        TEXT,               -- eventos de comunicacao: quanto tempo ficou sem dados
    UNIQUE (device_id, seq)
);

CREATE TABLE IF NOT EXISTS dispositivos (
    device_id TEXT PRIMARY KEY,
    online    INTEGER,
    ts_status TEXT
);
"""


@contextmanager
def conectar_db():
    """Uma conexao por operacao: a thread do MQTT grava, as rotas da API leem."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        with con:          # commit no fim (ou rollback se der excecao)
            yield con
    finally:
        con.close()


def criar_banco():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with conectar_db() as con:
        con.execute("PRAGMA journal_mode=WAL")   # leitura do dashboard nao trava a gravacao do MQTT
        con.executescript(DDL)
        colunas = [c[1] for c in con.execute("PRAGMA table_info(eventos)")]
        if "detalhe" not in colunas:       # banco criado antes desta versao
            con.execute("ALTER TABLE eventos ADD COLUMN detalhe TEXT")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc)


# ------------------------------------------------------------------ estado em memoria
class Contadores:
    def __init__(self):
        self.lock = threading.Lock()
        self.recebidas = 0
        self.gravadas = 0
        self.duplicadas = 0
        self.invalidas = 0
        self.broker_conectado = False
        self.broker_desde = None   # quando mudou o estado da conexao com o broker

    def somar(self, campo):
        with self.lock:
            setattr(self, campo, getattr(self, campo) + 1)

    def dict(self):
        return {"recebidas": self.recebidas, "gravadas": self.gravadas,
                "duplicadas_descartadas": self.duplicadas, "invalidas": self.invalidas}


cont = Contadores()


class MonitorTrafego:
    """Guarda em memoria as ultimas mensagens HTTP e MQTT que passaram pelo backend.

    Alimenta o painel de trafego do dashboard (um "Wireshark" simplificado). Nao e
    captura de pacotes de rede: registra o que o backend enviou e recebeu.
    """

    def __init__(self, capacidade=600):
        self.lock = threading.Lock()
        self.itens = deque(maxlen=capacidade)
        self.proximo_id = 1

    def registrar(self, proto, direcao, origem, destino, resumo, tamanho=0, detalhe=None, polling=False):
        with self.lock:
            self.itens.append({
                "id": self.proximo_id, "ts": iso(datetime.now(timezone.utc)), "proto": proto,
                "direcao": direcao, "origem": origem, "destino": destino, "resumo": resumo,
                "tamanho": tamanho, "polling": polling, "detalhe": detalhe,
            })
            self.proximo_id += 1

    def depois_de(self, ultimo_id):
        with self.lock:
            return [i for i in self.itens if i["id"] > ultimo_id]


trafego = MonitorTrafego()


def payload_legivel(bruto):
    try:
        return json.loads(bruto)
    except ValueError:
        return bruto.decode(errors="replace")


# ------------------------------------------------------------------ recepcao MQTT
def validar(p):
    """Confere o contrato do payload (docs/NOMEACAO.md). Retorna mensagem de erro ou None."""
    if p.get("schema") != SCHEMA_ACEITO:
        return f"schema {p.get('schema')!r} nao suportado"
    for campo in ("device_id", "seq", "ts_origem", "estado"):
        if campo not in p:
            return f"campo obrigatorio ausente: {campo}"
    if not isinstance(p["seq"], int) or p["seq"] <= 0:
        return "seq invalido"
    if p["estado"] not in ESTADOS or p.get("motivo") not in MOTIVOS:
        return "estado/motivo fora do dominio"
    if p.get("corrente_a") is not None and not isinstance(p["corrente_a"], (int, float)):
        return "corrente_a nao numerica"
    try:
        parse_iso(p["ts_origem"])
    except (ValueError, AttributeError):
        return "ts_origem nao e ISO-8601"
    return None


def gravar_leitura(p, recebido):
    ts_origem = parse_iso(p["ts_origem"])
    skew_ms = round((recebido - ts_origem).total_seconds() * 1000)
    with conectar_db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO leituras (device_id, seq, ts_origem, ts_recebimento, skew_ms, corrente_a, estado, motivo)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p["device_id"], p["seq"], iso(ts_origem), iso(recebido), skew_ms, p.get("corrente_a"), p["estado"], p.get("motivo")),
        )
    if cur.rowcount == 0:
        cont.somar("duplicadas")
        log.info(f"[DUP] {p['device_id']} seq={p['seq']} ja gravado -> descartado")
        return
    cont.somar("gravadas")
    origem = "BUFFER" if skew_ms > STALE_S * 1000 else "tempo real"
    log.info(f"[RX] {p['device_id']} seq={p['seq']} {p.get('corrente_a')} A {p['estado']} "
             f"skew={skew_ms} ms ({origem})")


def gravar_alarme(p, recebido):
    tipo = {"desarmado": "desarme", "emergencia": "emergencia", "operando": "rearme"}[p["estado"]]
    with conectar_db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO eventos (device_id, seq, tipo, ts_origem, ts_recebimento, estado, motivo, corrente_a)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p["device_id"], p["seq"], tipo, iso(parse_iso(p["ts_origem"])), iso(recebido),
             p["estado"], p.get("motivo"), p.get("corrente_a")),
        )
    if cur.rowcount:
        log.info(f"[ALARME] {p['device_id']} {tipo} motivo={p.get('motivo')} em {p['ts_origem']}")
    else:
        cont.somar("duplicadas")


def gravar_status(device_id, p, recebido):
    """status e retained: chega de novo a cada reconexao. So registra evento quando muda."""
    online = 1 if p.get("online") else 0
    with conectar_db() as con:
        linha = con.execute("SELECT online FROM dispositivos WHERE device_id = ?", (device_id,)).fetchone()
        if linha and linha["online"] == online:
            return
        con.execute("INSERT INTO dispositivos (device_id, online, ts_status) VALUES (?, ?, ?)"
                    " ON CONFLICT (device_id) DO UPDATE SET online = excluded.online, ts_status = excluded.ts_status",
                    (device_id, online, iso(recebido)))
        con.execute("INSERT INTO eventos (device_id, seq, tipo, ts_origem, ts_recebimento, estado)"
                    " VALUES (?, NULL, ?, ?, ?, ?)",
                    (device_id, "online" if online else "offline", p.get("ts_origem"), iso(recebido), p.get("estado")))
    log.info(f"[STATUS] {device_id} {'ONLINE' if online else 'OFFLINE (LWT ou desligamento)'}")


def on_message(cli, userdata, msg):
    recebido = datetime.now(timezone.utc)
    cont.somar("recebidas")
    partes = msg.topic.split("/")          # fabrica/<planta>/<device_id>/<tipo>
    trafego.registrar(
        "MQTT", "RX", f"{partes[2] if len(partes) > 2 else '?'} (via broker)", CLIENT_ID,
        f"PUBLISH {msg.topic} QoS{msg.qos}{' retained' if msg.retain else ''}", len(msg.payload),
        {"topico": msg.topic, "qos": msg.qos, "retained": bool(msg.retain), "payload": payload_legivel(msg.payload)})
    if len(partes) != 4:
        cont.somar("invalidas")
        return
    device_topico, tipo = partes[2], partes[3]
    try:
        p = json.loads(msg.payload)
    except ValueError:
        cont.somar("invalidas")
        log.info(f"[INVALIDA] {msg.topic}: JSON malformado")
        return
    if p.get("device_id") != device_topico:
        cont.somar("invalidas")
        log.info(f"[INVALIDA] {msg.topic}: device_id do payload ({p.get('device_id')}) difere do topico")
        return

    if tipo == "status":
        gravar_status(device_topico, p, recebido)
        return
    erro = validar(p)
    if erro:
        cont.somar("invalidas")
        log.info(f"[INVALIDA] {msg.topic}: {erro}")
        return
    if tipo == "corrente":
        gravar_leitura(p, recebido)
    elif tipo == "alarme":
        gravar_alarme(p, recebido)


def on_connect(cli, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.info(f"[MQTT] conexao RECUSADA pelo broker: {reason_code} (usuario/senha?)")
        trafego.registrar("MQTT", "conexao", CLIENT_ID, f"broker {MQTT_HOST}:{MQTT_PORT}",
                          f"CONNECT recusado: {reason_code}")
        return
    cont.broker_conectado = True
    cont.broker_desde = time.time()
    # sessao persistente: o broker guardou o que chegou enquanto o backend estava fora
    log.info(f"[MQTT] conectado ao broker (sessao existente={flags.session_present})")
    trafego.registrar("MQTT", "conexao", CLIENT_ID, f"broker {MQTT_HOST}:{MQTT_PORT}",
                      f"CONNECT ok · sessao existente={flags.session_present}")
    cli.subscribe([("fabrica/+/+/corrente", 1), ("fabrica/+/+/alarme", 1), ("fabrica/+/+/status", 1)])


def on_disconnect(cli, userdata, flags, reason_code, properties):
    if cont.broker_conectado:
        log.info("[MQTT] perdeu o broker -> dados vao ficar STALE; tentando reconectar")
        trafego.registrar("MQTT", "conexao", CLIENT_ID, f"broker {MQTT_HOST}:{MQTT_PORT}",
                          "conexao PERDIDA · tentando reconectar")
        cont.broker_desde = time.time()
    cont.broker_conectado = False


def criar_cliente_mqtt():
    cli = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                      client_id=CLIENT_ID, clean_session=False)
    cli.username_pw_set(MQTT_USER, MQTT_PASS)     # o broker recusa conexao anonima
    cli.on_connect = on_connect
    cli.on_disconnect = on_disconnect
    cli.on_message = on_message
    cli.reconnect_delay_set(min_delay=1, max_delay=5)
    cli.connect_async(MQTT_HOST, MQTT_PORT, keepalive=5)
    return cli


# --------------------------------------------------------------- consultas
def ultima_leitura(con, device_id):
    # "atual" = maior ts_origem, nao a ultima que chegou (o flush do buffer chega fora de ordem)
    return con.execute("SELECT * FROM leituras WHERE device_id = ? ORDER BY ts_origem DESC LIMIT 1",
                       (device_id,)).fetchone()


def calcular_lacunas(con, device_id):
    """Buracos na sequencia de seq dentro do mesmo boot (salto de boot nao e lacuna)."""
    linhas = con.execute(
        """SELECT anterior, seq FROM (
               SELECT seq, LAG(seq) OVER (ORDER BY seq) AS anterior FROM leituras WHERE device_id = ?
           ) WHERE anterior IS NOT NULL AND seq - anterior > 1 AND seq / ? = anterior / ?""",
        (device_id, BLOCO_BOOT, BLOCO_BOOT)).fetchall()
    return {"quantidade": len(linhas), "amostras_faltando": sum(l["seq"] - l["anterior"] - 1 for l in linhas),
            "trechos": [{"depois_de": l["anterior"], "antes_de": l["seq"]} for l in linhas[-5:]]}


def idade_s(linha, agora):
    return (agora - parse_iso(linha["ts_origem"])).total_seconds()


class VigiaStale(threading.Thread):
    """Registra quando os dados de cada dispositivo PARAM de chegar e quando VOLTAM.

    Grava no log (tela + logs/supervisorio.log) e na tabela eventos, que o dashboard mostra.
    O criterio e o mesmo da tela: ultima leitura com mais de STALE_S segundos (pela hora da medicao).
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.stale = {}
        self.parou_em = {}

    def registrar(self, con, dev, tipo, quando, detalhe):
        con.execute("INSERT INTO eventos (device_id, seq, tipo, ts_origem, ts_recebimento, detalhe)"
                    " VALUES (?, NULL, ?, ?, ?, ?)", (dev, tipo, iso(quando), iso(quando), detalhe))

    def run(self):
        primeira_volta = True
        while True:
            time.sleep(1)
            agora = datetime.now(timezone.utc)
            with conectar_db() as con:
                devices = [r[0] for r in con.execute("SELECT DISTINCT device_id FROM leituras")]
                for dev in devices:
                    linha = ultima_leitura(con, dev)
                    idade = idade_s(linha, agora)
                    agora_stale = idade > STALE_S
                    # na primeira volta so aprende o estado: dispositivo parado antes do backend subir
                    # nao gera um evento falso de "parou agora"
                    if not primeira_volta and agora_stale != self.stale.get(dev, False):
                        if agora_stale:
                            ultimo = parse_iso(linha["ts_origem"])
                            self.parou_em[dev] = ultimo
                            log.info(f"[SEM DADOS] {dev}: dados pararam de chegar; ultimo dado medido em "
                                     f"{linha['ts_origem']} (ha {idade:.1f}s) - dashboard em modo degradado")
                            self.registrar(con, dev, "sem_dados", ultimo,
                                           f"último dado medido há {idade:.0f} s")
                        else:
                            inicio = self.parou_em.pop(dev, None)
                            duracao = (agora - inicio).total_seconds() if inicio else None
                            texto = f"{duracao:.0f} s sem dados" if duracao is not None else "dados voltaram"
                            log.info(f"[DADOS VOLTARAM] {dev}: dado atual de novo ({texto}); "
                                     f"lacunas restantes: {calcular_lacunas(con, dev)['amostras_faltando']}")
                            self.registrar(con, dev, "dados_voltaram", agora, texto)
                    self.stale[dev] = agora_stale
            primeira_volta = False


# ------------------------------------------------------------------- API
cliente_mqtt = None


@asynccontextmanager
async def ciclo_de_vida(app):
    global cliente_mqtt
    criar_banco()
    cliente_mqtt = criar_cliente_mqtt()
    cliente_mqtt.loop_start()
    VigiaStale().start()
    log.info(f"[BACKEND] banco={DB_PATH} broker={MQTT_HOST}:{MQTT_PORT} stale>{STALE_S:.0f}s")
    yield
    cliente_mqtt.disconnect()
    cliente_mqtt.loop_stop()


app = FastAPI(title="Supervisorio moinho", lifespan=ciclo_de_vida)


# rotas que o dashboard e o celular consultam a cada 1-2 s: o painel pode esconde-las
ROTAS_POLLING = {"/api/estado", "/api/historico", "/api/eventos", "/api/dispositivos", "/api/resumo",
                 "/api/gateway/situacao"}
ROTAS_NAO_CAPTURADAS = {"/api/trafego", "/favicon.ico"}


@app.middleware("http")
async def capturar_http(request: Request, call_next):
    inicio = time.perf_counter()
    resposta = await call_next(request)
    caminho = request.url.path
    if caminho not in ROTAS_NAO_CAPTURADAS:
        ms = (time.perf_counter() - inicio) * 1000
        url = caminho + (f"?{request.url.query}" if request.url.query else "")
        cliente = request.client.host if request.client else "?"
        trafego.registrar(
            "HTTP", "req", cliente, f"backend :{request.url.port or 80}",
            f"{request.method} {url} -> {resposta.status_code} · {ms:.0f} ms",
            int(resposta.headers.get("content-length", 0)),
            {"metodo": request.method, "url": url, "status": resposta.status_code, "duracao_ms": round(ms, 1),
             "cliente": cliente, "user_agent": request.headers.get("user-agent", ""),
             "corpo_enviado": getattr(request.state, "corpo", None),
             "resposta": getattr(request.state, "resposta", None)},
            polling=caminho in ROTAS_POLLING)
    return resposta


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/celular")
def pagina_celular():
    return FileResponse(Path(__file__).parent / "static" / "celular.html")


@app.get("/api/trafego")
def listar_trafego(depois: int = 0):
    return trafego.depois_de(depois)


@app.get("/api/dispositivos")
def dispositivos():
    with conectar_db() as con:
        linhas = con.execute("SELECT device_id FROM leituras UNION SELECT device_id FROM dispositivos").fetchall()
    return sorted(l[0] for l in linhas)


@app.get("/api/resumo")
def resumo():
    """Ultima leitura de cada dispositivo (faixa de dispositivos no topo do dashboard)."""
    agora = datetime.now(timezone.utc)
    saida = []
    with conectar_db() as con:
        for (dev,) in con.execute("SELECT DISTINCT device_id FROM leituras ORDER BY device_id").fetchall():
            u = ultima_leitura(con, dev)
            idade = idade_s(u, agora)
            saida.append({"device_id": dev, "corrente_a": u["corrente_a"], "estado": u["estado"],
                          "motivo": u["motivo"], "idade_s": round(idade, 1), "stale": idade > STALE_S})
    return saida


def ip_na_rede_local():
    """IP do PC na rede Wi-Fi/cabo (para montar o endereco que o celular abre)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # nao envia nada: so escolhe a interface de saida
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/api/rede")
def rede(request: Request):
    ip, porta = ip_na_rede_local(), request.url.port or 80
    return {"ip": ip, "url_celular": f"http://{ip}:{porta}/celular"}


# ------------------------------------------------------------ ponte HTTP -> MQTT
# O navegador do celular nao fala MQTT puro (TCP); ele fala HTTP. O backend recebe a
# leitura por HTTP e publica no broker com QoS 1, no mesmo topico que um ESP32 usaria.
# Dali para frente o caminho e o mesmo: broker -> assinante -> banco -> dashboard.
def token_valido(request):
    """Authorization: Bearer <token>. Comparacao em tempo constante (hmac) para nao vazar o token."""
    if not GATEWAY_TOKEN:
        return False                       # sem token configurado, a ponte fica fechada
    cabecalho = request.headers.get("authorization", "")
    enviado = cabecalho[7:] if cabecalho.lower().startswith("bearer ") else ""
    return hmac.compare_digest(enviado.encode(), GATEWAY_TOKEN.encode())


@app.post("/api/gateway/{tipo}")
async def gateway(tipo: str, request: Request):
    try:
        corpo = await request.json()
    except ValueError:
        return JSONResponse({"erro": "JSON malformado"}, status_code=400)
    request.state.corpo = corpo

    def recusar(status, erro):
        request.state.resposta = {"erro": erro}
        return JSONResponse(request.state.resposta, status_code=status)

    if not token_valido(request):
        log.info(f"[SEGURANCA] gateway: envio RECUSADO de {request.client.host if request.client else '?'}"
                 " (sem token ou token errado)")
        return recusar(401, "não autorizado: falta a chave de acesso (token) ou ela está errada")
    if tipo not in ("corrente", "alarme", "status"):
        return recusar(404, f"tipo desconhecido: {tipo}")
    device_id = str(corpo.get("device_id", ""))
    if not DEVICE_ID_VALIDO.match(device_id):
        return recusar(422, "device_id invalido")
    erro = None if tipo == "status" else validar(corpo)
    if erro:
        return recusar(422, erro)
    if not cont.broker_conectado:
        return recusar(503, "broker MQTT inacessivel: guarde no buffer e tente de novo")

    topico = f"fabrica/{PLANTA_GATEWAY}/{device_id}/{tipo}"
    texto = json.dumps(corpo)
    info = cliente_mqtt.publish(topico, texto, qos=1, retain=(tipo == "status"))
    trafego.registrar("MQTT", "TX", f"{CLIENT_ID} (ponte HTTP)", "broker",
                      f"PUBLISH {topico} QoS1{' retained' if tipo == 'status' else ''}", len(texto),
                      {"topico": topico, "qos": 1, "retained": tipo == "status", "payload": corpo, "mid": info.mid})
    request.state.resposta = {"publicado": True, "topico": topico, "mid": info.mid,
                              "recebido_em": iso(datetime.now(timezone.utc))}
    return request.state.resposta


@app.get("/api/gateway/situacao")
def situacao_gateway(device_id: str, boot: int):
    """O que o PC ja gravou deste no, nesta sessao (bloco de seq do boot).

    Tambem diz se OUTRA fonte (outro bloco de seq) publicou como o mesmo device_id nos
    ultimos 5 s: e o caso do simulador ligado junto com o celular fazendo o moinho01.
    """
    inicio, fim = boot * BLOCO_BOOT, (boot + 1) * BLOCO_BOOT
    recente = iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    with conectar_db() as con:
        ult = con.execute("SELECT seq, skew_ms FROM leituras WHERE device_id = ? AND seq >= ? AND seq < ?"
                          " ORDER BY seq DESC LIMIT 1", (device_id, inicio, fim)).fetchone()
        total = con.execute("SELECT COUNT(*) FROM leituras WHERE device_id = ? AND seq >= ? AND seq < ?",
                            (device_id, inicio, fim)).fetchone()[0]
        outras = con.execute("SELECT COUNT(*) FROM leituras WHERE device_id = ? AND ts_recebimento >= ?"
                             " AND (seq < ? OR seq >= ?)", (device_id, recente, inicio, fim)).fetchone()[0]
        lacunas = calcular_lacunas(con, device_id)
    return {"gravadas": total, "ultimo_seq": ult["seq"] if ult else None,
            "ultimo_skew_ms": ult["skew_ms"] if ult else None,
            "lacunas": lacunas["amostras_faltando"], "outra_fonte_publicando": outras > 0,
            "broker_conectado": cont.broker_conectado}


@app.get("/api/estado")
def estado(device_id: str = "moinho01"):
    agora = datetime.now(timezone.utc)
    with conectar_db() as con:
        ult = ultima_leitura(con, device_id)
        alarme = con.execute(
            "SELECT * FROM eventos WHERE device_id = ? AND seq IS NOT NULL ORDER BY ts_origem DESC LIMIT 1",
            (device_id,)).fetchone()
        disp = con.execute("SELECT * FROM dispositivos WHERE device_id = ?", (device_id,)).fetchone()
        lacunas = calcular_lacunas(con, device_id)
    idade = idade_s(ult, agora) if ult else None
    return {
        "device_id": device_id,
        "agora": iso(agora),
        "stale_limite_s": STALE_S,
        "stale": idade is None or idade > STALE_S,
        "idade_s": None if idade is None else round(idade, 1),
        "ultima_leitura": dict(ult) if ult else None,
        "ultimo_alarme": dict(alarme) if alarme else None,
        "dispositivo_online": None if disp is None else bool(disp["online"]),
        "broker_conectado": cont.broker_conectado,
        "broker_desde_s": None if cont.broker_desde is None else round(time.time() - cont.broker_desde, 1),
        "contadores": cont.dict(),
        "lacunas": lacunas,
    }


@app.get("/api/historico")
def historico(device_id: str = "moinho01", segundos: int = 180):
    desde = iso(datetime.now(timezone.utc) - timedelta(seconds=segundos))
    with conectar_db() as con:
        linhas = con.execute(
            "SELECT seq, ts_origem, ts_recebimento, skew_ms, corrente_a, estado, motivo FROM leituras"
            " WHERE device_id = ? AND ts_origem >= ? ORDER BY ts_origem",
            (device_id, desde)).fetchall()
    return [dict(l) for l in linhas]


@app.get("/api/eventos")
def eventos(device_id: str = "moinho01", limite: int = 15):
    with conectar_db() as con:
        linhas = con.execute(
            "SELECT * FROM eventos WHERE device_id = ? ORDER BY COALESCE(ts_origem, ts_recebimento) DESC LIMIT ?",
            (device_id, limite)).fetchall()
    return [dict(l) for l in linhas]
