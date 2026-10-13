#!/usr/bin/env bash
# Envia um comando ao simulador (funciona mesmo com o broker fora: nao passa pela rede MQTT).
# Uso: scripts/comando.sh normal|sobrecorrente|falha_sensor|emergencia|liberar|rearmar
set -euo pipefail
cmd="${1:?uso: $0 normal|sobrecorrente|falha_sensor|emergencia|liberar|rearmar}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^moinho-simulador$'; then
  docker exec moinho-simulador sh -c "echo $cmd > /app/estado/comando.txt"
else
  pasta="$(dirname "$0")/../simulador/estado"
  mkdir -p "$pasta"
  echo "$cmd" > "$pasta/comando.txt"
fi
echo "comando enviado: $cmd"
