# Sobe tudo SEM Docker, cada parte numa janela propria do PowerShell:
#   BROKER MQTT, BACKEND (dashboard em http://localhost:8000) e SIMULADOR.
#
# Uso (na raiz do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SoBroker    # religa so o broker
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SemSimulador # o celular faz o moinho01
#
# Para simular a queda de comunicacao: feche a janela "BROKER MQTT".
param(
    [switch]$SoBroker,
    [switch]$SemSimulador,
    [switch]$SemNavegador
)

$raiz  = Split-Path -Parent $PSScriptRoot
$venv  = Join-Path $raiz '.venv'
$py    = Join-Path $venv 'Scripts\python.exe'
$amqtt = Join-Path $venv 'Scripts\amqtt.exe'

if (-not (Test-Path $amqtt)) {
    Write-Host 'Primeira execucao: criando o ambiente Python (.venv) e instalando dependencias...'
    if (-not (Test-Path $py)) { python -m venv $venv }
    & $py -m pip install --quiet -r (Join-Path $raiz 'backend\requirements.txt') amqtt
}

function Abrir-Janela([string]$titulo, [string]$pasta, [string]$comando) {
    $script = "`$host.UI.RawUI.WindowTitle = '$titulo'; Set-Location '$pasta'; $comando"
    Start-Process powershell -ArgumentList '-NoExit', '-Command', $script
}

Abrir-Janela 'BROKER MQTT' $raiz "& '$amqtt' -c '$raiz\mosquitto\amqtt-local.yaml'"
if ($SoBroker) { Write-Host 'Broker religado.'; exit }

Start-Sleep -Seconds 3
Abrir-Janela 'BACKEND' "$raiz\backend" "& '$py' -m uvicorn app:app --host 0.0.0.0 --port 8000 --no-access-log"
if (-not $SemSimulador) { Abrir-Janela 'SIMULADOR' "$raiz\simulador" "& '$py' simulador.py" }

Start-Sleep -Seconds 5
if (-not $SemNavegador) { Start-Process 'http://localhost:8000' }
Write-Host 'Pronto: dashboard em http://localhost:8000'
