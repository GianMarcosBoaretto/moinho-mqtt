"""
Simulador do no de medicao do moinho01 (substitui o ESP32 na bancada, sem hardware).

Reproduz a MESMA logica e o MESMO contrato de topicos/payload do firmware:

  1. PROTECAO (local, a cada 100 ms): le a corrente, decide e atua no rele.
     Nao usa rede nenhuma. Se o broker cair, ela continua igual.
  2. MONITORAMENTO (depois, a cada 1 s ou na mudanca de estado): monta o
     payload e coloca no buffer circular; o buffer e esvaziado quando ha
     conexao com o broker.

Comandos (escreva uma palavra em estado/comando.txt):
  normal | sobrecorrente | falha_sensor   -> muda a carga simulada do moinho
  emergencia | liberar                    -> pressiona / solta a botoeira
  rearmar                                 -> rearme manual apos desarme
"""

import json
import logging
import math
import os
import random
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------- configuracao
DEVICE_ID = os.getenv("DEVICE_ID", "moinho01")
PLANTA = os.getenv("PLANTA", "planta1")
CLIENT_ID = os.getenv("CLIENT_ID", f"sim-{DEVICE_ID}")  # o ESP32 real usa esp32-moinho01
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
# usuario e senha do no no broker: vem do ambiente (.env), nunca do codigo. Ver docs/SEGURANCA.md
MQTT_USER = os.getenv("MQTT_USER", "no-moinho01")
MQTT_PASS = os.getenv("MQTT_PASS", "")

PREFIXO = f"fabrica/{PLANTA}/{DEVICE_ID}"
TOPICO_CORRENTE = f"{PREFIXO}/corrente"
TOPICO_STATUS = f"{PREFIXO}/status"
TOPICO_ALARME = f"{PREFIXO}/alarme"

LIMITE_A = float(os.getenv("LIMITE_A", "100"))       # corrente de desarme
AMOSTRAS_CONFIRMACAO = 3                              # 3 x 100 ms acima do limite = desarme
PERIODO_PROTECAO_S = 0.1
PERIODO_TELEMETRIA_S = 1.0
TAMANHO_BUFFER = int(os.getenv("TAMANHO_BUFFER", "300"))

PASTA_ESTADO = Path(os.getenv("PASTA_ESTADO", Path(__file__).parent / "estado"))
ARQ_COMANDO = PASTA_ESTADO / "comando.txt"
ARQ_BOOT = PASTA_ESTADO / "boot.json"

SCHEMA = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("simulador")


def agora_iso():
    """Timestamp de origem: UTC, ISO-8601, milissegundos."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ protecao
class Protecao:
    """Logica de desarme. Nao conhece MQTT. E a mesma do firmware (protecao.h)."""

    def __init__(self, limite_a, confirmacao):
        self.limite_a = limite_a
        self.confirmacao = confirmacao
        self.disparo = None        # None | "sobrecorrente" | "falha_sensor" (fica travado ate rearmar)
        self.botoeira = False      # botoeira de emergencia pressionada
        self.cont_sobre = 0
        self.cont_falha = 0

    @property
    def rele_fechado(self):
        # Em hardware a botoeira esta EM SERIE com o rele: abre o circuito sozinha,
        # mesmo que o software trave. Aqui so refletimos isso.
        return not self.botoeira and self.disparo is None

    @property
    def estado(self):
        if self.botoeira:
            return "emergencia"
        if self.disparo:
            return "desarmado"
        return "operando"

    def situacao(self):
        return (self.estado, self.disparo)

    def avaliar(self, corrente_a):
        """Recebe a leitura (None = sensor invalido) e decide se desarma."""
        if self.disparo is None:
            self.cont_falha = self.cont_falha + 1 if corrente_a is None else 0
            acima = corrente_a is not None and corrente_a > self.limite_a
            self.cont_sobre = self.cont_sobre + 1 if acima else 0
            if self.cont_falha >= self.confirmacao:
                self.disparo = "falha_sensor"      # sem medicao nao ha protecao: desarma (fail-safe)
            elif self.cont_sobre >= self.confirmacao:
                self.disparo = "sobrecorrente"

    def rearmar(self):
        if self.botoeira:
            log.info("[PROTECAO] rearme recusado: botoeira de emergencia ainda pressionada")
            return False
        self.disparo = None
        self.cont_sobre = self.cont_falha = 0
        return True


class SensorSimulado:
    """Faz o papel do potenciometro/sensor de corrente."""

    def __init__(self):
        self.modo = "normal"
        self.t0 = time.monotonic()

    def ler(self, motor_ligado):
        if self.modo == "falha_sensor":
            return None                                   # leitura fora da faixa / fio rompido
        if not motor_ligado:
            return round(random.uniform(0.0, 0.3), 1)     # rele aberto: motor parado
        base = 128.0 if self.modo == "sobrecorrente" else 72.0
        t = time.monotonic() - self.t0
        return round(base + 4 * math.sin(t / 8) + random.gauss(0, 1.5), 1)


# ------------------------------------------------------------- sequencia (seq)
class Sequencia:
    """seq monotonico por dispositivo, que sobrevive a reinicio.

    seq = boot * 1_000_000_000 + contador. O numero de boot fica gravado em disco
    (no ESP32, em NVS). Sem isso, um reinicio voltaria o seq para 1 e o backend
    descartaria os dados novos como "duplicados".
    """

    BLOCO = 1_000_000_000

    def __init__(self, arquivo):
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        boot = 0
        if arquivo.exists():
            boot = json.loads(arquivo.read_text()).get("boot", 0)
        self.boot = boot + 1
        arquivo.write_text(json.dumps({"boot": self.boot}))
        self.contador = 0

    def proximo(self):
        self.contador += 1
        return self.boot * self.BLOCO + self.contador


# --------------------------------------------------------------- monitoramento
class Telemetria:
    """Camada de monitoramento: MQTT + buffer circular. A protecao nunca espera por ela."""

    def __init__(self):
        self.buffer = deque(maxlen=TAMANHO_BUFFER)
        self.descartadas = 0
        self.conectado = False
        self.desde_desconexao = time.monotonic()   # comeca "sem broker" ate a 1a conexao
        self.status_atual = None

        self.cli = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
            clean_session=False,          # sessao persistente no broker
        )
        # LWT: se o no sumir sem se despedir, o broker publica "offline" (retained)
        self.cli.will_set(TOPICO_STATUS, json.dumps({"schema": SCHEMA, "device_id": DEVICE_ID, "online": False}),
                          qos=1, retain=True)
        self.cli.username_pw_set(MQTT_USER, MQTT_PASS)   # o broker recusa conexao anonima
        self.cli.reconnect_delay_set(min_delay=1, max_delay=5)
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect

    def iniciar(self):
        log.info(f"[MQTT] conectando em {MQTT_HOST}:{MQTT_PORT} como client_id={CLIENT_ID}")
        self.cli.connect_async(MQTT_HOST, MQTT_PORT, keepalive=5)
        self.cli.loop_start()             # rede roda em thread propria; o laco de protecao nao bloqueia

    def _on_connect(self, cli, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.info(f"[MQTT] conexao RECUSADA pelo broker: {reason_code} (confira usuario/senha no .env)")
            return
        fora = f" (ficou fora {time.monotonic() - self.desde_desconexao:.1f}s)" if self.desde_desconexao else ""
        log.info(f"[MQTT] conectado ao broker{fora}; buffer com {len(self.buffer)} mensagens para reenviar")
        self.conectado = True
        self.desde_desconexao = None
        if self.status_atual:
            self.publicar_status(self.status_atual)

    def _on_disconnect(self, cli, userdata, flags, reason_code, properties):
        if self.conectado:
            log.info("[MQTT] broker inacessivel -> monitoramento suspenso. "
                     "A PROTECAO LOCAL CONTINUA ATUANDO; amostras vao para o buffer.")
        self.conectado = False
        self.desde_desconexao = self.desde_desconexao or time.monotonic()

    def enfileirar(self, topico, payload):
        """Tudo passa pelo buffer: preserva a ordem e o ts_origem/seq originais."""
        if len(self.buffer) == self.buffer.maxlen:
            self.descartadas += 1
            perdida = self.buffer[0][1]
            log.info(f"[BUFFER] cheio ({TAMANHO_BUFFER}): descartando a mais antiga seq={perdida['seq']}")
        self.buffer.append((topico, payload))

    def esvaziar(self):
        if not self.conectado or not self.buffer:
            return
        reenvio = len(self.buffer) > 1
        enviadas = 0
        while self.conectado and self.buffer:
            topico, payload = self.buffer[0]
            info = self.cli.publish(topico, json.dumps(payload), qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                break                      # caiu no meio: o que sobrou fica no buffer
            self.buffer.popleft()
            enviadas += 1
        if reenvio:
            log.info(f"[BUFFER] flush: {enviadas} mensagens reenviadas com ts_origem/seq originais "
                     f"(restam {len(self.buffer)})")

    def publicar_status(self, status):
        self.status_atual = status
        if self.conectado:                # status e retained: so o mais recente importa, nao vai pro buffer
            self.cli.publish(TOPICO_STATUS, json.dumps(status), qos=1, retain=True)

    def encerrar(self):
        if self.conectado:
            off = {"schema": SCHEMA, "device_id": DEVICE_ID, "online": False}
            self.cli.publish(TOPICO_STATUS, json.dumps(off), qos=1, retain=True).wait_for_publish(2)
        self.cli.disconnect()
        self.cli.loop_stop()


# ----------------------------------------------------------------- laco principal
def ler_comando():
    try:
        cmd = ARQ_COMANDO.read_text().strip().lower()
    except FileNotFoundError:
        return None
    ARQ_COMANDO.unlink(missing_ok=True)
    return cmd or None


def aplicar_comando(cmd, sensor, protecao):
    if cmd in ("normal", "sobrecorrente", "falha_sensor"):
        sensor.modo = cmd
    elif cmd == "emergencia":
        protecao.botoeira = True
    elif cmd == "liberar":
        protecao.botoeira = False
    elif cmd == "rearmar":
        protecao.rearmar()
    else:
        log.info(f"[CMD] comando desconhecido: {cmd!r}")
        return
    log.info(f"[CMD] {cmd}")


def montar_payload(seq, corrente, protecao):
    return {
        "schema": SCHEMA,
        "device_id": DEVICE_ID,
        "seq": seq,
        "ts_origem": agora_iso(),
        "corrente_a": corrente,
        "estado": protecao.estado,
        "motivo": protecao.disparo,
    }


def montar_status(protecao, ts_origem):
    return {"schema": SCHEMA, "device_id": DEVICE_ID, "online": True,
            "estado": protecao.estado, "motivo": protecao.disparo, "ts_origem": ts_origem}


def main():
    sensor = SensorSimulado()
    protecao = Protecao(LIMITE_A, AMOSTRAS_CONFIRMACAO)
    seq = Sequencia(ARQ_BOOT)
    tele = Telemetria()

    def sair(*_):
        log.info("[SIM] encerrando")
        tele.encerrar()
        sys.exit(0)

    signal.signal(signal.SIGTERM, sair)
    signal.signal(signal.SIGINT, sair)

    log.info(f"[SIM] {DEVICE_ID} boot={seq.boot} limite={LIMITE_A:.0f} A buffer={TAMANHO_BUFFER}")
    tele.status_atual = montar_status(protecao, agora_iso())   # publicado assim que conectar
    tele.iniciar()

    proxima_telemetria = 0.0
    proximo_aviso = 0.0
    while True:
        inicio = time.monotonic()
        antes = protecao.situacao()

        cmd = ler_comando()
        if cmd:
            aplicar_comando(cmd, sensor, protecao)

        # ===== 1. PROTECAO LOCAL: mede, decide e atua. Nenhuma chamada de rede aqui. =====
        corrente = sensor.ler(protecao.rele_fechado)
        protecao.avaliar(corrente)
        mudou = protecao.situacao() != antes
        if mudou:
            rede = "online" if tele.conectado else "SEM REDE"
            log.info(f"[PROTECAO] estado={protecao.estado} motivo={protecao.disparo} corrente={corrente} A "
                     f"rele={'FECHADO' if protecao.rele_fechado else 'ABERTO'} (decidido localmente, {rede})")

        # ===== 2. MONITORAMENTO: so depois da decisao, e sem bloquear =====
        if mudou or inicio >= proxima_telemetria:
            proxima_telemetria = inicio + PERIODO_TELEMETRIA_S
            payload = montar_payload(seq.proximo(), corrente, protecao)
            tele.enfileirar(TOPICO_CORRENTE, payload)
            if mudou:
                tele.enfileirar(TOPICO_ALARME, payload)   # mesmo seq: o alarme e a leitura que o causou
                tele.publicar_status(montar_status(protecao, payload["ts_origem"]))
        tele.esvaziar()

        if not tele.conectado and inicio >= proximo_aviso:
            proximo_aviso = inicio + 5
            fora = time.monotonic() - (tele.desde_desconexao or inicio)
            log.info(f"[PROTECAO] sem broker ha {fora:.0f}s - protecao local ATIVA: estado={protecao.estado} "
                     f"corrente={corrente} A | buffer {len(tele.buffer)}/{TAMANHO_BUFFER} "
                     f"descartadas={tele.descartadas}")

        time.sleep(max(0.0, PERIODO_PROTECAO_S - (time.monotonic() - inicio)))


if __name__ == "__main__":
    main()
