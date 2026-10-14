# Envia um comando ao simulador rodando SEM Docker (versao Windows do comando.sh).
# Nao passa pela rede MQTT, entao funciona tambem com o broker fora.
#
# Uso: powershell -ExecutionPolicy Bypass -File scripts\comando.ps1 sobrecorrente
param(
    [Parameter(Mandatory)]
    [ValidateSet('normal', 'sobrecorrente', 'falha_sensor', 'emergencia', 'liberar', 'rearmar')]
    [string]$Comando
)

$pasta = Join-Path (Split-Path -Parent $PSScriptRoot) 'simulador\estado'
New-Item -ItemType Directory -Force $pasta | Out-Null
Set-Content -Path (Join-Path $pasta 'comando.txt') -Value $Comando -Encoding Ascii
Write-Host "comando enviado: $Comando"
