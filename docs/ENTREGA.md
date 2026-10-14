# Entrega — Comunicação, nomeação, sincronização, replicação e consistência

Resumo para anexar à documentação do Projeto Integrador. Cada item do enunciado aponta para
onde está a resposta completa neste repositório.

**Projeto:** monitoramento e proteção por sobrecorrente de um moinho industrial.
**Grupo:** Fernando Grahl Zuffo, Gian Marcos Boaretto, Felipe Castilhos e Luiz Felipe Kauê da Silva.

## Fluxo escolhido

```
nó de medição ──MQTT (QoS 1)──▶ broker Mosquitto ──MQTT──▶ serviço supervisório ──▶ SQLite ──▶ dashboard
  (ESP32, simulador                                            (FastAPI)                       (navegador)
   ou celular)
     origem                 comunicação                   componente que recebe           ação: o operador vê
```

O nó **mede, decide e desarma localmente**: a proteção não depende da rede. O fluxo
implementado é a camada de monitoramento. O dado é **usado** pelo destino: o supervisório
valida, deduplica, grava e mostra corrente, estado, alarmes e histórico. O dashboard também
avisa quando o dado está desatualizado.

## O que foi pedido × onde está

| # | Pedido | Resposta curta | Detalhe |
|---|---|---|---|
| 1 | Arquitetura atualizada, com componentes e comunicações | diagrama com nó, broker, supervisório, banco, navegador e celular; protocolo de cada enlace e quem é cliente/servidor; diagrama de sequência da falha | [arquitetura.md](arquitetura.md) |
| 2 | Fluxo funcionando, com evidência de envio, recebimento e uso | teste automatizado com 14 verificações, todas aprovadas; logs do nó e do supervisório; JSON da API em cada etapa; prints | [EVIDENCIAS.md](../EVIDENCIAS.md), pasta [evidencias/](../evidencias/) |
| 3 | Nomeação | tópicos `fabrica/<planta>/<device_id>/<tipo>`; `client_id` único por conexão; `device_id` igual no tópico, no payload e na chave do banco; payload JSON com `schema` versionado | [NOMEACAO.md](NOMEACAO.md) |
| 3 | Sincronização | `ts_origem` gerado na medição (NTP no ESP32); backend grava `ts_recebimento` e o atraso (skew); tudo ordenado por `ts_origem`; `seq` monotônico para deduplicar e achar lacunas | [SINCRONIZACAO.md](SINCRONIZACAO.md) |
| 4 | Dados durante a falha | proteção segue local; telemetria vai para um buffer circular de 300 amostras (5 min); acima disso perde a mais antiga e a lacuna fica visível | [FALHA_COMUNICACAO.md §1](FALHA_COMUNICACAO.md) |
| 4 | O que o usuário vê | banner "Comunicação perdida há Xs — último dado recebido às HH:MM:SS"; valores em cinza com "VALOR ANTIGO"; faixa "sem dados" no gráfico; causa provável | [FALHA_COMUNICACAO.md §2](FALHA_COMUNICACAO.md) |
| 4 | Quando a comunicação volta | buffer reenviado em ordem com `seq`/`ts_origem` originais; duplicados descartados pela chave `(device_id, seq)`; histórico reconstruído no horário certo | [FALHA_COMUNICACAO.md §3](FALHA_COMUNICACAO.md) |
| 4 | Como se identifica dado desatualizado | STALE quando `agora − ts_origem > 5 s`, independente de quando o dado chegou; LWT como complemento | [FALHA_COMUNICACAO.md §4](FALHA_COMUNICACAO.md) |
| — | Replicação e consistência | buffer do nó, sessão persistente, status retained e banco como cópias; replicação assíncrona; convergência por identidade única + escrita idempotente + ordem por `ts_origem` = consistência eventual | [DECISOES.md D3 e D3b](DECISOES.md) |
| — | Decisões e justificativas | proteção local, MQTT com QoS 1, consistência eventual, ordenação por origem, tecnologias, ponte do celular | [DECISOES.md](DECISOES.md) |
| — | Código | simulador, firmware ESP32, backend, dashboard, página do celular, scripts de teste | este repositório ([README](../README.md)) |

## Resultado do teste de falha (14/10/2026)

Execução de `scripts/teste_falha_local.ps1` (Windows, sem Docker), com o broker derrubado
por 32 s:

| Verificação | Resultado |
|---|---|
| Comunicação normal: dado atual, leituras gravadas | ✅ |
| Broker fora + sobrecorrente: **o nó desarmou localmente, sem rede** | ✅ |
| Supervisório marcou o dado como STALE e sabe que perdeu o broker | ✅ |
| Supervisório ainda mostra o último estado conhecido (OPERANDO), em cinza — o desarme não tinha chegado | ✅ |
| Broker de volta: 31 mensagens do buffer reenviadas | ✅ |
| Desarme registrado com o **horário em que aconteceu** (durante a falha) | ✅ |
| Nenhuma lacuna de `seq`; histórico ordenado por `ts_origem` | ✅ |
| 2 duplicados enviados de propósito, 2 descartados | ✅ |

Logs completos: [evidencias/teste-falha-local-2026-10-14/](../evidencias/teste-falha-local-2026-10-14/).

## Base para as próximas etapas

O contrato (tópicos com `device_id`, `schema` versionado, `seq` com bloco de boot) já
permite acrescentar mais moinhos, comandos remotos e autenticação/TLS sem refazer o fluxo.
O que ficou fora desta entrega está listado em [DECISOES.md D6](DECISOES.md).
