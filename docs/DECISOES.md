# Decisões e justificativas

## D1. A proteção é local (no nó de medição)

**Decisão:** o ESP32 mede, decide e aciona o relé. A botoeira de emergência fica
fisicamente **em série** com o relé.

**Por quê:**
- Sobrecorrente em motor de moinho se resolve em centenas de milissegundos. Uma decisão
  que dependesse de Wi-Fi → broker → backend → resposta teria latência variável e vários
  pontos de falha no caminho.
- Se a rede cai, uma proteção remota simplesmente deixa de existir. A local continua igual.
- A botoeira em série desliga o motor mesmo com o ESP32 travado, porque não passa por software.
- **Fail-safe:** sensor inválido (fora da faixa) por 3 leituras seguidas também desarma
  (`falha_sensor`), porque sem medição não há proteção. Contato da botoeira aberto ou fio
  solto é lido como emergência.

**Como o código garante isso:**
- No simulador, a decisão vem antes de qualquer chamada de rede no laço, e a rede roda
  em outra thread (`loop_start`).
- No firmware, a proteção é uma tarefa FreeRTOS com prioridade maior que o `loop()`. Se o
  `loop()` travar tentando conectar no broker, ela continua no horário, e entrega amostras
  por uma fila com timeout zero: fila cheia perde telemetria, nunca atrasa a proteção.

## D2. MQTT para o monitoramento

**Por quê MQTT, e não HTTP do ESP32 para o backend:**
- **Publish/subscribe:** o nó não precisa saber quem consome os dados. Backend,
  dashboard e um futuro historiador assinam sem mexer no firmware.
- **Conexão de saída do nó:** o ESP32 é cliente; não precisa abrir porta nem ter IP fixo.
- **Recursos prontos** para o que esta entrega exige: QoS 1, sessão persistente, LWT
  (detecção de queda do nó) e retained (último status para quem chega depois).
- **Leve:** cabeçalho de 2 bytes, bibliotecas maduras para ESP32 e Python.

**Por quê QoS 1 e não 0 ou 2:**
- QoS 0 perderia leituras em qualquer oscilação.
- QoS 2 ("exatamente uma vez") custa 4 mensagens por publicação e ainda dependeria do
  broker guardar estado. A deduplicação por `(device_id, seq)` no banco dá o mesmo
  resultado mais barato: "pelo menos uma vez" + idempotência = "efetivamente uma vez".

## D3. Consistência eventual é aceitável aqui

Durante uma falha, o banco fica **para trás** do que aconteceu no moinho. Isso é aceitável porque:

1. **Nenhuma decisão de segurança lê o banco.** O desarme acontece no nó antes de
   qualquer dado sair dele. O banco serve para histórico, telas e análise.
2. **O banco converge sozinho.** Quando a comunicação volta, o buffer reenvia tudo com
   `ts_origem`/`seq` originais, e o histórico fica igual ao que seria sem a falha. A
   única exceção é uma falha maior que o buffer (5 min), que aparece como lacuna
   explícita, e não como dado errado.
3. **A tela não mente sobre isso.** Enquanto o banco está atrasado, o dashboard diz que
   está ("comunicação perdida há Xs", valores em cinza). O risco da consistência eventual
   seria o operador agir sobre dado velho achando que é atual; o STALE elimina esse risco.

Consistência forte exigiria que o nó esperasse a confirmação do banco antes de seguir.
Isso colocaria a rede no caminho da proteção, que é exatamente o que D1 proíbe.

## D4. Ordenação por `ts_origem`, não por chegada

Explicado em [SINCRONIZACAO.md](SINCRONIZACAO.md). Resumo: a ordem de chegada é um
artefato da rede (o flush do buffer chega fora de ordem em relação ao tempo real). O que
interessa ao processo é quando a corrente foi medida.

## D5. Tecnologias do protótipo

| Escolha | Motivo |
|---|---|
| Mosquitto em Docker | broker de referência, uma linha no compose, persistência nativa |
| Python + paho-mqtt (simulador) | roda sem hardware, mesma lógica do firmware, fácil de forçar falhas |
| FastAPI + SQLite (backend) | um arquivo só, sem servidor de banco; `UNIQUE` e funções de janela (`LAG`) bastam |
| HTML puro + polling de 1 s | sem build nem framework; 1 s é o mesmo período da telemetria |
| Buffer de 300 amostras | 5 min a 1 Hz cobre a maioria das quedas de Wi-Fi/broker; ~10 KB de RAM no ESP32 |
| STALE em 5 s | 5 leituras perdidas seguidas; folga para oscilação sem alarme falso |
| `seq` com bloco de boot | reinício do nó não colide com a chave de deduplicação |

## D6. Fora do escopo desta entrega

Autenticação/TLS no broker, comandos remotos (rearme pela tela), vários dispositivos
ao mesmo tempo no dashboard e retenção/limpeza do banco. O contrato (tópicos com
`device_id`, `schema` versionado) já permite acrescentar isso depois.
