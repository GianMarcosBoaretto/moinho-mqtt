"""Mostra tudo o que passa no broker em fabrica/# (prova de que o no esta transmitindo).

O broker exige usuario e senha: o espiao entra como 'supervisorio', com a senha do .env.
Com --sem-senha, tenta entrar como um intruso e mostra a recusa do broker.

Uso: python scripts/espiar.py [host]                 (Ctrl+C para sair)
     python scripts/espiar.py --sem-senha [host]
"""
import sys
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

args = [a for a in sys.argv[1:] if not a.startswith("--")]
SEM_SENHA = "--sem-senha" in sys.argv
HOST = args[0] if args else "localhost"


def senha_do_env():
    env = Path(__file__).resolve().parent.parent / ".env"
    for linha in env.read_text(encoding="utf-8").splitlines():
        if linha.startswith("MQTT_SENHA_SUPERVISORIO="):
            return linha.split("=", 1)[1].strip()
    sys.exit("MQTT_SENHA_SUPERVISORIO nao encontrada no .env")


def ao_receber(cli, userdata, msg):
    retido = "  [retained]" if msg.retain else ""
    print(f"{datetime.now():%H:%M:%S}  {msg.topic}{retido}\n          {msg.payload.decode() or '(vazio)'}")


def ao_conectar(cli, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"RECUSADO pelo broker: {reason_code}")
        cli.disconnect()
        return
    print(f"conectado em {HOST}:1883 como supervisorio, escutando fabrica/# ...")
    cli.subscribe("fabrica/#", qos=1)


cli = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="espiao")
if SEM_SENHA:
    print("tentando conectar SEM usuario e senha...")
else:
    cli.username_pw_set("supervisorio", senha_do_env())
cli.on_connect = ao_conectar
cli.on_message = ao_receber
cli.connect(HOST, 1883)
try:
    cli.loop_forever()
except KeyboardInterrupt:
    pass
