# Teste de falha de comunicacao SEM Docker (Windows) - mesmo roteiro do teste_falha.sh:
#
#   1. sobe broker (amqtt), backend e simulador e coleta dados normais
#   2. derruba o broker e, SEM rede, forca uma sobrecorrente: a protecao local tem que desarmar
#   3. confere o modo degradado (STALE)
#   4. religa o broker: o buffer do no e reenviado e o historico reconstruido
#   5. reenvia uma leitura repetida de proposito: o backend descarta pela chave (device_id, seq)
#   6. para a ORIGEM (o simulador) e religa: aviso na tela e volta sozinho
#   7. seguranca: conexao anonima recusada, ACL de topicos e API sem chave recusada
#   8. log: confere o registro de quando os dados pararam e quando voltaram
#
# Usa senhas proprias, aleatorias, so desta execucao (nao le nem altera o .env).
#
# Uso (na raiz do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\teste_falha_local.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\teste_falha_local.ps1 -PortaHttp 8010 -PortaMqtt 1884
#
# Usa banco e estado proprios (na pasta de saida): nao mexe nos dados do rodar-local.
# Saida: logs\local-<data-hora>\ com os logs de cada componente, os JSON de cada etapa e resultado.txt
param(
    [int]$PortaHttp = 8085,
    [int]$PortaMqtt = 1883
)

$ErrorActionPreference = 'Stop'
$raiz  = Split-Path -Parent $PSScriptRoot
$py    = Join-Path $raiz '.venv\Scripts\python.exe'
$amqtt = Join-Path $raiz '.venv\Scripts\amqtt.exe'
$saida = Join-Path $raiz ("logs\local-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$api   = "http://127.0.0.1:$PortaHttp"
New-Item -ItemType Directory -Force $saida | Out-Null
$script:falhas = 0
$script:resultado = New-Object System.Collections.Generic.List[string]

function Passo($t)  { Write-Host ""; Write-Host "================ $t ================" -ForegroundColor Cyan; $script:resultado.Add("`n== $t") }
function Ok($t)     { Write-Host "  [OK]    $t" -ForegroundColor Green; $script:resultado.Add("  [OK]    $t") }
function Falhou($t) { Write-Host "  [FALHA] $t" -ForegroundColor Red; $script:resultado.Add("  [FALHA] $t"); $script:falhas++ }
function Info($t)   { Write-Host "  $t"; $script:resultado.Add("  $t") }
function Conferir($cond, $ok, $falha) { if ($cond) { Ok $ok } else { Falhou $falha } }

if (-not (Test-Path $amqtt)) { throw 'Rode antes o scripts\rodar-local.ps1 uma vez (ele cria o .venv).' }
foreach ($p in $PortaHttp, $PortaMqtt) {
    if (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue) {
        throw "A porta $p ja esta em uso. Feche o rodar-local (ou outro programa) ou use -PortaHttp/-PortaMqtt."
    }
}

# ambiente dos processos filhos (herdam deste processo)
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$env:MQTT_HOST = '127.0.0.1'
$env:MQTT_PORT = "$PortaMqtt"
$env:DB_PATH = Join-Path $saida 'teste.db'
$env:PASTA_ESTADO = Join-Path $saida 'simulador-estado'
$env:CLIENT_ID = $null
$env:LOG_PATH = Join-Path $saida 'supervisorio.log'

# senhas aleatorias so deste teste + arquivo de hashes do broker
$senhaNo = [guid]::NewGuid().ToString('N'); $senhaSup = [guid]::NewGuid().ToString('N'); $token = [guid]::NewGuid().ToString('N')
$senhasArq = Join-Path $saida 'senhas.txt'
$env:T_NO = $senhaNo; $env:T_SUP = $senhaSup; $env:T_ARQ = $senhasArq
& $py -c "import os; from pwdlib import PasswordHash; from pwdlib.hashers.argon2 import Argon2Hasher; h=PasswordHash((Argon2Hasher(),)); open(os.environ['T_ARQ'],'w').write('no-moinho01:'+h.hash(os.environ['T_NO'])+'\nsupervisorio:'+h.hash(os.environ['T_SUP'])+'\n')"
$env:T_NO = $null; $env:T_SUP = $null

$yaml = Join-Path $saida 'broker.yaml'
$senhasYaml = $senhasArq -replace '\\', '/'
@"
listeners:
  default:
    type: tcp
    bind: 127.0.0.1:$PortaMqtt
plugins:
  amqtt.plugins.authentication.FileAuthPlugin:
    password_file: $senhasYaml
  amqtt.plugins.topic_checking.TopicAccessControlListPlugin:
    acl:
      supervisorio: ["fabrica/#"]
    publish_acl:
      no-moinho01: ["fabrica/planta1/moinho01/#"]
      supervisorio: ["fabrica/#"]
"@ | Set-Content -Encoding Ascii $yaml

$script:nBroker = 0
function Iniciar-Broker {
    $script:nBroker++
    Start-Process $amqtt -ArgumentList '-c', "`"$yaml`"" -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $saida "broker-$($script:nBroker).log") `
        -RedirectStandardError (Join-Path $saida "broker-$($script:nBroker).err.log") | Out-Null
    Start-Sleep -Seconds 3
}
function Parar([string]$marca) {
    # o python do .venv abre um processo filho: para tudo que tiver a marca na linha de comando
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$marca*" -and $_.Name -notlike 'powershell*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Api($rota) { Invoke-RestMethod "$api$rota" -TimeoutSec 5 }
function Salvar($nome) {
    (Invoke-WebRequest "$api/api/estado?device_id=moinho01" -UseBasicParsing).Content |
        Set-Content -Encoding UTF8 (Join-Path $saida "$nome.json")
    Info "estado salvo em $nome.json"
}
function Comando($c) { Set-Content -Encoding Ascii (Join-Path $env:PASTA_ESTADO 'comando.txt') $c }
function LogSim { Get-Content (Join-Path $saida 'simulador.log') -Encoding UTF8 }
$script:nSim = 0
function Iniciar-Simulador {
    $script:nSim++
    $env:MQTT_USER = 'no-moinho01'; $env:MQTT_PASS = $senhaNo
    $sufixo = if ($script:nSim -eq 1) { '' } else { "-$($script:nSim)" }
    Start-Process $py -ArgumentList 'simulador.py' -WorkingDirectory (Join-Path $raiz 'simulador') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $saida "simulador$sufixo.log") -RedirectStandardError (Join-Path $saida "simulador$sufixo.err.log") | Out-Null
}
# executa um trecho python com paho (os valores vao por variavel de ambiente)
function Py([string]$codigo) { & $py -c $codigo }
function StatusHttp([string]$metodo, [string]$rota, [string]$corpo, [string]$chave) {
    $cab = @{}
    if ($chave) { $cab['Authorization'] = "Bearer $chave" }
    try {
        (Invoke-WebRequest "$api$rota" -Method $metodo -Body $corpo -ContentType 'application/json' -Headers $cab -UseBasicParsing -TimeoutSec 5).StatusCode
    } catch { [int]$_.Exception.Response.StatusCode }
}

try {
    # ---------------------------------------------------------------------------
    Passo '1/8 subindo broker (com senha), backend e simulador'
    Iniciar-Broker
    $env:MQTT_USER = 'supervisorio'; $env:MQTT_PASS = $senhaSup; $env:GATEWAY_TOKEN = $token
    Start-Process $py -ArgumentList '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', "$PortaHttp", '--no-access-log' `
        -WorkingDirectory (Join-Path $raiz 'backend') -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $saida 'backend.log') -RedirectStandardError (Join-Path $saida 'backend.err.log') | Out-Null
    $env:GATEWAY_TOKEN = $null
    Iniciar-Simulador
    foreach ($i in 1..30) { try { Api '/api/estado' | Out-Null; break } catch { Start-Sleep 1 } }
    Info 'coletando 12 s de dados normais...'
    Start-Sleep -Seconds 12
    Salvar '01_normal'
    $e = Api '/api/estado'
    Conferir (-not $e.stale) 'dado atual (nao STALE)' 'dado deveria estar atual'
    Conferir ($e.contadores.gravadas -ge 8) "backend gravou $($e.contadores.gravadas) leituras" 'poucas leituras gravadas'

    # ---------------------------------------------------------------------------
    Passo '2/8 derrubando o broker (falha de COMUNICACAO)'
    $inicioFalha = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss')
    Parar $yaml
    Info "broker parado as $(Get-Date -Format HH:mm:ss)"
    Start-Sleep -Seconds 5

    Passo '3/8 sobrecorrente SEM REDE: a protecao local tem que desarmar'
    Comando 'sobrecorrente'
    Start-Sleep -Seconds 3
    $linha = LogSim | Where-Object { $_ -match 'desarmado motivo=sobrecorrente.*SEM REDE' } | Select-Object -First 1
    Conferir ($null -ne $linha) 'simulador desarmou localmente com o broker fora' 'nao achei o desarme local no log do simulador'
    if ($linha) { Info "sim| $linha" }
    Start-Sleep -Seconds 4
    Salvar '02_degradado'
    $e = Api '/api/estado'
    Conferir $e.stale "backend marcou o dado como STALE (idade $($e.idade_s) s)" 'dado deveria estar STALE'
    Conferir (-not $e.broker_conectado) 'backend sabe que perdeu o broker' 'broker_conectado deveria ser falso'
    Conferir ($e.ultima_leitura.estado -eq 'operando') 'supervisorio ainda NAO sabe do desarme (o dado nao chegou) - por isso o banner' 'o desarme nao deveria ter chegado'
    Info 'aguardando mais 15 s com o broker fora (enche o buffer do simulador)...'
    Start-Sleep -Seconds 15
    LogSim | Where-Object { $_ -match 'sem broker' } | Select-Object -Last 2 | ForEach-Object { Info "sim| $_" }

    # ---------------------------------------------------------------------------
    Passo '4/8 religando o broker: volta sozinho, sem reiniciar nada'
    Iniciar-Broker
    Info "broker de volta as $(Get-Date -Format HH:mm:ss); aguardando o flush do buffer..."
    Start-Sleep -Seconds 8
    Salvar '03_reconstruido'
    $e = Api '/api/estado'
    Conferir (-not $e.stale) 'dado atual de novo' 'dado deveria estar atual'
    Conferir ($e.ultima_leitura.motivo -eq 'sobrecorrente') 'o desarme feito durante a falha chegou' 'desarme nao chegou'
    Conferir ($e.lacunas.amostras_faltando -eq 0) 'nenhuma lacuna de seq: historico completo' "ha $($e.lacunas.amostras_faltando) amostras faltando"
    $hist = Api '/api/historico?device_id=moinho01&segundos=600'
    $doBuffer = @($hist | Where-Object { $_.skew_ms -gt 5000 }).Count
    Conferir ($doBuffer -gt 10) "$doBuffer leituras chegaram do buffer com ts_origem/seq originais" 'poucas leituras vieram do buffer'
    $eventos = Api '/api/eventos?device_id=moinho01&limite=50'   # atribuir antes: o PS 5.1 entrega a lista como um item so
    $desarme = $eventos | Where-Object { $_.tipo -eq 'desarme' } | Select-Object -First 1
    Conferir ($desarme -and $desarme.ts_origem -gt $inicioFalha) "desarme registrado com o horario em que aconteceu: $($desarme.ts_origem)" 'desarme sem o horario da falha'
    $ordenado = ($hist.ts_origem -join '|') -eq (($hist.ts_origem | Sort-Object) -join '|')
    Conferir $ordenado 'historico ordenado por ts_origem' 'historico fora de ordem'
    LogSim | Where-Object { $_ -match '\[MQTT\] conectado|\[BUFFER\] flush' } | Select-Object -Last 2 | ForEach-Object { Info "sim| $_" }

    # ---------------------------------------------------------------------------
    Passo '5/8 idempotencia: reenviando de proposito uma leitura ja gravada'
    $antes = (Api '/api/estado').contadores.duplicadas_descartadas
    $l = $hist[-5]
    $json = @{ schema = 1; device_id = 'moinho01'; seq = $l.seq; ts_origem = $l.ts_origem; corrente_a = $l.corrente_a; estado = $l.estado; motivo = $l.motivo } | ConvertTo-Json -Compress
    Info "payload repetido: $json"
    # o JSON vai por variavel de ambiente: o PS 5.1 tira as aspas de argumentos passados a programas
    $env:PAYLOAD_DUP = $json; $env:T_SUP = $senhaSup
    $pub = "import os,paho.mqtt.publish as p; [p.single('fabrica/planta1/moinho01/corrente', os.environ['PAYLOAD_DUP'], qos=1, hostname='127.0.0.1', port=$PortaMqtt, auth={'username':'supervisorio','password':os.environ['T_SUP']}) for _ in range(2)]"
    & $py -c $pub
    Start-Sleep -Seconds 2
    $depois = (Api '/api/estado').contadores.duplicadas_descartadas
    Conferir ($depois -ge $antes + 2) "2 duplicados descartados pela chave (device_id, seq) ($antes -> $depois)" 'duplicados nao foram descartados'

    Comando 'normal'; Start-Sleep -Milliseconds 500; Comando 'rearmar'; Start-Sleep -Seconds 2
    Conferir ((Api '/api/estado').ultima_leitura.estado -eq 'operando') 'rearmado: OPERANDO de novo' 'nao rearmou'

    # ---------------------------------------------------------------------------
    Passo '6/8 parando a ORIGEM (simulador) e religando'
    Parar 'simulador.py'
    Info "simulador parado as $(Get-Date -Format HH:mm:ss)"
    Start-Sleep -Seconds 10
    Salvar '04_origem_parada'
    $e = Api '/api/estado'
    Conferir $e.stale "sem a origem, o dado ficou desatualizado (idade $($e.idade_s) s); o ultimo valor continua na tela" 'dado deveria estar desatualizado'
    Conferir ($e.dispositivo_online -eq $false) 'o broker avisou que o no saiu (LWT): dispositivo offline' 'LWT nao chegou'
    Iniciar-Simulador
    Info "simulador religado as $(Get-Date -Format HH:mm:ss)"
    Start-Sleep -Seconds 8
    $e = Api '/api/estado'
    Conferir (-not $e.stale) 'origem de volta: dado atual de novo, sem reiniciar o supervisorio' 'dado deveria estar atual'
    Conferir ($e.dispositivo_online -eq $true) 'dispositivo online de novo' 'dispositivo deveria estar online'

    # ---------------------------------------------------------------------------
    Passo '7/8 seguranca'
    $env:T_PORTA = "$PortaMqtt"
    $anon = Py "import os,time,paho.mqtt.client as m; r=[]; c=m.Client(m.CallbackAPIVersion.VERSION2, client_id='intruso'); c.on_connect=lambda *a: r.append(a[3]); c.connect('127.0.0.1', int(os.environ['T_PORTA'])); c.loop_start(); time.sleep(2); c.loop_stop(); print('RECUSADO' if (not r or r[0].is_failure) else 'ACEITO')"
    Conferir ($anon -eq 'RECUSADO') 'broker RECUSA conexao sem usuario e senha' "conexao anonima foi $anon"
    $env:T_NO = $senhaNo
    Py "import os,json,paho.mqtt.publish as p; p.single('fabrica/planta1/moinho99/corrente', json.dumps({'schema':1,'device_id':'moinho99','seq':1,'ts_origem':'2026-10-26T12:00:00.000Z','corrente_a':99.0,'estado':'operando','motivo':None}), qos=1, hostname='127.0.0.1', port=int(os.environ['T_PORTA']), auth={'username':'no-moinho01','password':os.environ['T_NO']})"
    $env:T_NO = $null
    Start-Sleep -Seconds 2
    $disp = Api '/api/dispositivos'
    Conferir (-not ($disp -contains 'moinho99')) 'ACL: o no-moinho01 nao consegue publicar como outro moinho (moinho99 nao apareceu)' 'ACL falhou: moinho99 foi gravado'
    $leitura = '{"schema":1,"device_id":"celteste","seq":1000000001,"ts_origem":"2026-10-26T12:00:00.000Z","corrente_a":70.0,"estado":"operando","motivo":null}'
    $semChave = StatusHttp 'POST' '/api/gateway/corrente' $leitura ''
    $chaveErrada = StatusHttp 'POST' '/api/gateway/corrente' $leitura 'chave-errada'
    $chaveCerta = StatusHttp 'POST' '/api/gateway/corrente' $leitura $token
    Conferir ($semChave -eq 401) "API do celular sem chave: HTTP $semChave (recusado)" "sem chave deveria dar 401, deu $semChave"
    Conferir ($chaveErrada -eq 401) "API do celular com chave errada: HTTP $chaveErrada (recusado)" "chave errada deveria dar 401, deu $chaveErrada"
    Conferir ($chaveCerta -eq 200) "API do celular com a chave certa: HTTP $chaveCerta" "chave certa deveria dar 200, deu $chaveCerta"
    $versionados = git -C $raiz ls-files .env mosquitto/senhas-amqtt.txt firmware/moinho01/secrets.h
    Conferir (-not $versionados) 'nenhum arquivo de senha esta versionado no Git (.env, senhas, secrets.h)' "arquivo de senha versionado: $versionados"

    # ---------------------------------------------------------------------------
    Passo '8/8 log de quando os dados pararam e voltaram'
    $logSup = Get-Content $env:LOG_PATH -Encoding UTF8
    $parou = @($logSup | Where-Object { $_ -match '\[SEM DADOS\] moinho01' })
    $voltou = @($logSup | Where-Object { $_ -match '\[DADOS VOLTARAM\] moinho01' })
    Conferir ($parou.Count -ge 2) "log registrou $($parou.Count) vezes que os dados PARARAM (broker e origem)" 'faltou registro de parada no log'
    Conferir ($voltou.Count -ge 2) "log registrou $($voltou.Count) vezes que os dados VOLTARAM" 'faltou registro de volta no log'
    $parou + $voltou | Sort-Object | ForEach-Object { Info "log| $_" }
    $evs = Api '/api/eventos?device_id=moinho01&limite=50'
    $tipos = @($evs | ForEach-Object { $_.tipo })
    Conferir (($tipos -contains 'sem_dados') -and ($tipos -contains 'dados_voltaram')) 'eventos "Dados pararam" e "Dados voltaram" gravados no banco (aparecem no dashboard)' 'eventos de comunicacao nao gravados'
}
catch {
    Falhou "erro no teste: $_"
}
finally {
    Parar 'simulador.py'
    Parar "--port $PortaHttp"
    Parar $yaml
    Start-Sleep -Seconds 1
    Get-ChildItem $saida -Filter '*.err.log' | Where-Object { $_.Length -eq 0 } | Remove-Item
}

$fim = if ($script:falhas -eq 0) { 'RESULTADO: todas as verificacoes passaram' } else { "RESULTADO: $($script:falhas) verificacao(oes) falharam" }
$script:resultado.Add("`n$fim")
$script:resultado | Set-Content -Encoding UTF8 (Join-Path $saida 'resultado.txt')
Write-Host ""
Write-Host $fim -ForegroundColor $(if ($script:falhas -eq 0) { 'Green' } else { 'Red' })
Write-Host "Logs e JSON em $saida"
exit $script:falhas
