"""Mostra tudo o que passa no broker em fabrica/# (prova de que o no esta transmitindo).

Uso: python scripts/espiar.py [host]      (Ctrl+C para sair)
"""
import sys
from datetime import datetime

import paho.mqtt.client as mqtt

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"


def ao_receber(cli, userdata, msg):
    retido = "  [retained]" if msg.retain else ""
    print(f"{datetime.now():%H:%M:%S}  {msg.topic}{retido}\n          {msg.payload.decode() or '(vazio)'}")


def ao_conectar(cli, userdata, flags, reason_code, properties):
    print(f"conectado em {HOST}:1883, escutando fabrica/# ...")
    cli.subscribe("fabrica/#", qos=1)


cli = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="espiao")
cli.on_connect = ao_conectar
cli.on_message = ao_receber
cli.connect(HOST, 1883)
try:
    cli.loop_forever()
except KeyboardInterrupt:
    pass
