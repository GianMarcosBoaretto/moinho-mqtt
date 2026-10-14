#!/usr/bin/env bash
# Teste de falha de comunicacao, de ponta a ponta.
#
#   1. sobe tudo e deixa publicar dados normais
#   2. derruba o broker (docker stop) e, SEM rede, forca uma sobrecorrente:
#      a protecao local tem que desarmar mesmo assim
#   3. mostra o dashboard em estado degradado (STALE)
#   4. sobe o broker de novo: o buffer do no e reenviado e o historico reconstruido
#   5. reenvia uma mensagem repetida de proposito: o backend descarta pela chave (device_id, seq)
#
# Uso:  ./scripts/teste_falha.sh            (na raiz do repositorio)
# Saida: pasta logs/<data-hora>/ com os logs e os JSON de cada etapa.

set -uo pipefail
cd "$(dirname "$0")/.."

API="http://localhost:8085"
DEVICE="moinho01"
SAIDA="logs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SAIDA"
FALHAS=0

passo()  { echo; echo "================ $* ================"; }
print()  { echo ">>> PRINT: $*"; }
ok()     { echo "  [OK]    $*"; }
falhou() { echo "  [FALHA] $*"; FALHAS=$((FALHAS + 1)); }

# Consulta a API de dentro do container do backend (tem python; o host pode nao ter).
# Uso: api_py '<expressao python sobre e (estado), h (historico) ou ev (eventos)>'
api_py() {
  docker exec -i moinho-backend python - "$1" <<'PY'
import json, sys, urllib.request
base = "http://localhost:8085"
get = lambda p: json.load(urllib.request.urlopen(base + p))
e = get("/api/estado?device_id=moinho01")
h = get("/api/historico?device_id=moinho01&segundos=600")
ev = get("/api/eventos?device_id=moinho01&limite=50")
print(eval(sys.argv[1]))
PY
}

salvar_estado() {
  docker exec moinho-backend python -c \
    "import urllib.request;print(urllib.request.urlopen('$API/api/estado?device_id=$DEVICE').read().decode())" \
    > "$SAIDA/$1.json"
  echo "  estado salvo em $SAIDA/$1.json"
}

# --------------------------------------------------------------------------------
passo "1/6 subindo broker, backend e simulador"
docker compose up -d --build
echo -n "  aguardando o backend"
for _ in $(seq 1 60); do
  if docker exec moinho-backend python -c "import urllib.request;urllib.request.urlopen('$API/api/estado')" 2>/dev/null; then break; fi
  echo -n "."; sleep 1
done
echo
scripts/comando.sh normal >/dev/null
scripts/comando.sh rearmar >/dev/null
echo "  coletando 15 s de dados normais..."
sleep 15
salvar_estado 01_normal
[ "$(api_py 'e["stale"]')" = "False" ] && ok "dado atual (nao STALE)" || falhou "dado deveria estar atual"
print "dashboard normal em $API (corrente ~72 A, OPERANDO, sem banner)"

# --------------------------------------------------------------------------------
passo "2/6 derrubando o broker (docker stop moinho-broker)"
INICIO_FALHA=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker stop moinho-broker >/dev/null
echo "  broker parado as $(date +%H:%M:%S)"
sleep 5

passo "3/6 sobrecorrente SEM REDE: a protecao local tem que desarmar"
scripts/comando.sh sobrecorrente
sleep 3
if docker logs --since 10s moinho-simulador 2>&1 | grep -q "desarmado motivo=sobrecorrente.*SEM REDE"; then
  ok "simulador desarmou localmente com o broker fora"
else
  falhou "nao achei o desarme local no log do simulador"
fi
docker logs --since 10s moinho-simulador 2>&1 | grep -E "PROTECAO|CMD" | sed 's/^/  sim| /'
sleep 7
salvar_estado 02_degradado
[ "$(api_py 'e["stale"]')" = "True" ] && ok "backend marcou o dado como STALE" || falhou "dado deveria estar STALE"
[ "$(api_py 'e["broker_conectado"]')" = "False" ] && ok "backend sabe que perdeu o broker" || falhou "broker_conectado deveria ser False"
[ "$(api_py 'e["ultima_leitura"]["estado"]')" = "operando" ] \
  && ok "supervisorio ainda NAO sabe do desarme (o dado nao chegou) - por isso o banner" \
  || falhou "o desarme nao deveria ter chegado com o broker fora"
print "dashboard DEGRADADO: banner vermelho 'comunicacao perdida ha Xs', valores em cinza, faixa 'sem dados' no grafico"
echo "  aguardando mais 15 s com o broker fora (enche o buffer do simulador)..."
sleep 15
docker logs --since 20s moinho-simulador 2>&1 | grep "sem broker" | tail -2 | sed 's/^/  sim| /'
print "terminal com 'docker logs moinho-simulador': linhas '[PROTECAO] sem broker ... protecao local ATIVA'"

# --------------------------------------------------------------------------------
passo "4/6 subindo o broker de novo (docker start moinho-broker)"
docker start moinho-broker >/dev/null
echo "  broker de volta as $(date +%H:%M:%S); aguardando o flush do buffer..."
sleep 10
salvar_estado 03_reconstruido
[ "$(api_py 'e["stale"]')" = "False" ] && ok "dado atual de novo" || falhou "dado deveria estar atual"
[ "$(api_py 'e["ultima_leitura"]["motivo"]')" = "sobrecorrente" ] && ok "estado desarmado/sobrecorrente chegou" || falhou "desarme nao chegou"
[ "$(api_py 'e["lacunas"]["amostras_faltando"]')" = "0" ] && ok "nenhuma lacuna de seq: historico completo" || falhou "ha lacunas de seq"
api_py "'  leituras com ts_origem durante a falha: ' + str(sum(1 for x in h if x['ts_origem'] >= '$INICIO_FALHA' and x['skew_ms'] > 5000))"
api_py "'  desarme registrado com ts_origem = ' + next(x['ts_origem'] for x in ev if x['tipo'] == 'desarme')"
docker logs --since 15s moinho-simulador 2>&1 | grep -E "MQTT|BUFFER" | sed 's/^/  sim| /'
docker logs --since 15s moinho-backend 2>&1 | grep -E "BUFFER|ALARME|RECUPERADO|MQTT" | head -8 | sed 's/^/  bkd| /'
print "dashboard RECONSTRUIDO: trecho laranja no grafico (veio do buffer) e o desarme na tabela de eventos com o horario real"

# --------------------------------------------------------------------------------
passo "5/6 idempotencia: reenviando de proposito uma leitura ja gravada"
DUP_ANTES=$(api_py 'e["contadores"]["duplicadas_descartadas"]')
PAYLOAD=$(api_py 'json.dumps({"schema": 1, "device_id": "moinho01", **{k: h[-5][k] for k in ("seq", "ts_origem", "corrente_a", "estado", "motivo")}})')
echo "  payload repetido: $PAYLOAD"
docker exec moinho-broker mosquitto_pub -q 1 -t "fabrica/planta1/$DEVICE/corrente" -m "$PAYLOAD"
docker exec moinho-broker mosquitto_pub -q 1 -t "fabrica/planta1/$DEVICE/corrente" -m "$PAYLOAD"
sleep 2
DUP_DEPOIS=$(api_py 'e["contadores"]["duplicadas_descartadas"]')
[ "$DUP_DEPOIS" -ge $((DUP_ANTES + 2)) ] && ok "2 duplicados descartados pela chave (device_id, seq)" || falhou "duplicados nao foram descartados"
docker logs --since 5s moinho-backend 2>&1 | grep DUP | sed 's/^/  bkd| /'

# --------------------------------------------------------------------------------
passo "6/6 rearme e coleta dos logs"
scripts/comando.sh normal >/dev/null
scripts/comando.sh rearmar >/dev/null
sleep 3
docker logs moinho-simulador > "$SAIDA/simulador.log" 2>&1
docker logs moinho-backend   > "$SAIDA/backend.log" 2>&1
docker logs moinho-broker    > "$SAIDA/broker.log" 2>&1
echo "  logs em $SAIDA/"

echo
if [ "$FALHAS" -eq 0 ]; then
  echo "RESULTADO: todas as verificacoes passaram"
else
  echo "RESULTADO: $FALHAS verificacao(oes) falharam - ver acima"
fi
exit "$FALHAS"
