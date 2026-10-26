# Sobe tudo SEM Docker, cada parte numa janela propria do PowerShell:
#   BROKER MQTT, BACKEND (dashboard em http://localhost:8085) e SIMULADOR.
#
# Uso (na raiz do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SoBroker    # religa so o broker
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SemSimulador # o celular faz o moinho01
#   powershell -ExecutionPolicy Bypass -File scripts\rodar-local.ps1 -SoSimulador  # religa so o simulador
#
# Para simular a queda de comunicacao: feche a janela "BROKER MQTT" (ou "SIMULADOR").
#
# Senhas: ficam no arquivo .env (fora do Git). Na primeira execucao ele e criado com
# senhas aleatorias. Cada janela recebe so a senha dela, por variavel de ambiente.
param(
    [switch]$SoBroker,
    [switch]$SoSimulador,
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

# cria o .env (se faltar) e o arquivo de hashes que o broker le
& $py (Join-Path $raiz 'scripts\preparar_segredos.py')
if ($LASTEXITCODE -ne 0) { throw 'Falha ao preparar os segredos (.env).' }
$segredos = @{}
Get-Content (Join-Path $raiz '.env') | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } |
    ForEach-Object { $segredos[$Matches[1]] = $Matches[2].Trim() }

function Abrir-Janela([string]$titulo, [string]$pasta, [string]$comando) {
    $script = "`$host.UI.RawUI.WindowTitle = '$titulo'; Set-Location '$pasta'; $comando"
    Start-Process powershell -ArgumentList '-NoExit', '-Command', $script
}

if ($SoSimulador) {
    $env:MQTT_USER = 'no-moinho01'; $env:MQTT_PASS = $segredos['MQTT_SENHA_NO']
    Abrir-Janela 'SIMULADOR' "$raiz\simulador" "& '$py' simulador.py"
    $env:MQTT_USER = $null; $env:MQTT_PASS = $null
    Write-Host 'Simulador religado.'; exit
}

Abrir-Janela 'BROKER MQTT' $raiz "& '$amqtt' -c '$raiz\mosquitto\amqtt-local.yaml'"
if ($SoBroker) { Write-Host 'Broker religado.'; exit }

Start-Sleep -Seconds 3
# cada janela herda as variaveis definidas logo antes de abrir
$env:MQTT_USER = 'supervisorio'
$env:MQTT_PASS = $segredos['MQTT_SENHA_SUPERVISORIO']
$env:GATEWAY_TOKEN = $segredos['GATEWAY_TOKEN']
Abrir-Janela 'BACKEND' "$raiz\backend" "& '$py' -m uvicorn app:app --host 0.0.0.0 --port 8085 --no-access-log"

if (-not $SemSimulador) {
    $env:MQTT_USER = 'no-moinho01'
    $env:MQTT_PASS = $segredos['MQTT_SENHA_NO']
    $env:GATEWAY_TOKEN = $null
    Abrir-Janela 'SIMULADOR' "$raiz\simulador" "& '$py' simulador.py"
}
$env:MQTT_USER = $null; $env:MQTT_PASS = $null; $env:GATEWAY_TOKEN = $null

Start-Sleep -Seconds 5
if (-not $SemNavegador) { Start-Process 'http://localhost:8085' }
Write-Host 'Pronto: dashboard em http://localhost:8085'
Write-Host 'Chave do celular (pagina /celular): valor de GATEWAY_TOKEN no arquivo .env'
