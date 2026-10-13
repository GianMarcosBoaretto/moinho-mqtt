# Comportamento em falha de comunicação

Princípio: **a proteção não depende da rede.** O nó de medição decide e aciona o relé
localmente. Wi-Fi, MQTT, backend e banco são só monitoramento. Uma falha de comunicação
deixa o operador **sem informação**, mas não deixa o moinho **sem proteção**.

## Mecanismos usados

| Mecanismo | Onde | Para quê |
|---|---|---|
| QoS 1 | todas as publicações e assinaturas | entrega "pelo menos uma vez" com confirmação (PUBACK) |
| Sessão persistente (`clean_session=false`) | nó e backend | o broker guarda as mensagens QoS 1 de quem está desconectado |
| Persistência do Mosquitto | `mosquitto.conf` | sessões e retained sobrevivem a reinício do broker |
| LWT `online:false` (retained) | nó | o broker avisa que o nó sumiu sem se despedir |
| Status retained | nó | quem conecta depois recebe na hora o último status |
| Buffer circular de 300 amostras | nó | guarda a telemetria enquanto não há broker |
| `UNIQUE(device_id, seq)` | banco | reenvio não duplica dado |
| STALE quando `agora − ts_origem > 5 s` | backend/dashboard | nunca mostrar dado velho como atual |

## 1. O que acontece com os dados durante a falha

- **No nó:** a tarefa de proteção continua medindo a cada 100 ms, decidindo e
  acionando o relé, igual a antes. Ela não chama nada de rede. Cada amostra de telemetria
  (1 por segundo, mais as de mudança de estado) entra no **buffer circular** com seu
  `seq` e seu `ts_origem` originais. O log mostra isso a cada 5 s:

  ```
  [PROTECAO] sem broker ha 10s - protecao local ATIVA: estado=desarmado corrente=0.2 A | buffer 11/300 descartadas=0
  ```

  Um desarme no meio da falha acontece normalmente e é registrado no log como
  "decidido localmente, SEM REDE".
- **Capacidade:** 300 amostras = 5 minutos a 1 amostra/s. Se a falha passar disso,
  a mais **antiga** é descartada (o log avisa) e vira uma lacuna permanente de `seq`,
  visível no dashboard. Preferimos perder o dado mais velho a perder o mais recente.
- **No broker:** se só o backend caiu, o broker guarda as mensagens QoS 1 na sessão
  persistente dele (até `max_queued_messages`). Se o broker caiu, quem segura os dados é
  o buffer do nó.
- **No banco:** nada é gravado durante a falha. O banco fica temporariamente
  incompleto e converge quando a comunicação volta.

## 2. O que o usuário vê

Quando a leitura mais recente passa de 5 s de idade, o dashboard entra em **modo degradado**:

- **banner vermelho:** "⚠ Comunicação perdida há 13s — último dado recebido às 09:29:17";
- **a causa provável**, na linha de baixo:
  - "O supervisório não alcança o broker MQTT" (o backend perdeu a conexão), ou
  - "O broker informou que o nó saiu da rede (LWT/offline)", ou
  - "Nenhuma leitura nova do nó chegou ao supervisório";
- **corrente e estado em cinza**, com "VALOR ANTIGO" ao lado. O valor continua na tela
  para referência, mas não parece atual;
- **no gráfico**, uma faixa cinza "sem dados" do último ponto até agora;
- o lembrete fixo: "A proteção do moinho não depende desta tela".

Se o próprio backend cair, o navegador não consegue buscar dados e mostra
"Supervisório (backend) inacessível", também com os valores em cinza.

## 3. O que acontece quando a comunicação volta

1. O nó reconecta (tentativa a cada 1–5 s) e publica o status `online` (retained).
2. O buffer é esvaziado **na ordem**, cada mensagem com o `seq` e o `ts_origem` de quando
   foi medida:
   ```
   [MQTT] conectado ao broker (ficou fora 17.1s); buffer com 18 mensagens para reenviar
   [BUFFER] flush: 18 mensagens reenviadas com ts_origem/seq originais (restam 0)
   ```
3. O backend grava cada uma. Pelo `skew_ms` grande, ele sabe que veio do buffer:
   ```
   [RX] moinho01 seq=1000000016 130.8 A desarmado skew=12827 ms (BUFFER)
   [ALARME] moinho01 desarme motivo=sobrecorrente em 2026-10-13T12:29:22.820Z
   [RECUPERADO] moinho01: dado atual de novo (idade 0.5s); lacunas restantes: 0
   ```
4. Como tudo é ordenado por `ts_origem`, o histórico fica **reconstruído no lugar
   certo**: o gráfico mostra o trecho da falha em laranja, e o desarme aparece na tabela
   de eventos com o **horário em que aconteceu**, não com o horário em que chegou.
5. Duplicados — um PUBACK perdido no meio do flush, por exemplo — são descartados pela
   chave `(device_id, seq)` e contados em "duplicadas descartadas".

## 4. Como o sistema identifica um dado desatualizado

- **Regra:** um dado é STALE quando `agora − ts_origem > 5 s`. A leitura chega a cada
  1 s, então 5 s são 5 leituras seguidas faltando.
- Usa-se o `ts_origem` (quando foi medido), não o `ts_recebimento`. Um dado velho que
  acaba de chegar do buffer **continua** velho.
- A "leitura atual" é a de maior `ts_origem`. Uma rajada de dados antigos no flush não
  substitui um dado mais novo.
- O backend calcula isso a cada consulta (`/api/estado` devolve `stale`, `idade_s` e
  `stale_limite_s`). Uma thread de vigia registra no log as transições `[STALE]` e
  `[RECUPERADO]`, que servem de evidência.
- O LWT complementa, mas **não substitui**: quando o broker cai (o caso do teste), não há
  broker para publicar o LWT. Só a idade do dado pega esse caso. Por isso o critério
  principal é a idade.
