# Sincronização e ordem dos dados

## Dois relógios, dois carimbos

| Carimbo | Quem gera | Significa |
|---|---|---|
| `ts_origem` | o nó (ESP32 com NTP; simulador com o relógio UTC da máquina) | quando a corrente **foi medida** |
| `ts_recebimento` | o backend, ao receber a mensagem | quando o dado **chegou** ao supervisório |
| `skew_ms` | o backend: `ts_recebimento − ts_origem` | atraso da entrega (rede + buffer) |

Em operação normal o `skew_ms` fica em poucos milissegundos. Depois de uma falha de
comunicação, as leituras que vieram do buffer chegam com `skew_ms` de vários segundos —
é assim que o dashboard sabe pintá-las de laranja ("reconstruído do buffer").

Um `skew_ms` **negativo** ou grande demais com a rede normal indica relógio do nó errado
(NTP não sincronizou). O valor fica gravado em toda leitura justamente para isso aparecer.

## Ordem: sempre por `ts_origem`

O backend **nunca** usa a ordem de chegada:

- histórico: `ORDER BY ts_origem`;
- "leitura atual": a de **maior** `ts_origem`, não a última que chegou;
- eventos: `ORDER BY ts_origem`.

Por que importa: quando a comunicação volta, o buffer despeja de uma vez leituras de
minutos atrás, misturadas com leituras novas. Se a tela mostrasse "a última que chegou",
o operador veria um valor velho como se fosse o atual.

`ts_origem` é gravado sempre no mesmo formato (`AAAA-MM-DDTHH:MM:SS.mmmZ`, UTC), então
a ordenação de texto no SQLite é também a ordenação cronológica.

## `seq`: deduplicação e lacunas

O `seq` é monotônico por dispositivo (ver [NOMEACAO.md](NOMEACAO.md)) e tem dois usos:

1. **Deduplicar.** QoS 1 garante "pelo menos uma vez": se o PUBACK se perde, o mesmo
   dado chega duas vezes. `UNIQUE(device_id, seq)` + `INSERT OR IGNORE` faz a segunda
   cópia ser descartada, e o resultado é "efetivamente uma vez". O contador
   `duplicadas_descartadas` aparece no dashboard.
2. **Detectar lacunas.** Se depois do `seq` 41 vem o 44, faltam 42 e 43. O backend
   calcula as lacunas com `LAG(seq)` dentro do mesmo bloco de boot. Lacuna que se fecha
   sozinha = dado que estava no buffer e chegou depois. Lacuna que não fecha = dado
   perdido de verdade (buffer estourou).

## Como cada lado obtém a hora

**Simulador:** `datetime.now(timezone.utc)` no instante da leitura.

**ESP32:** `configTime(0, 0, "a.st1.ntp.br", "pool.ntp.org")`. A amostra guarda o relógio
monotônico (`esp_timer_get_time()`, em µs desde o boot) e o `ts_origem` é calculado na
hora de publicar:

```
ts_origem = agora_ntp − (agora_monotonico − instante_monotonico_da_amostra)
```

Com isso, amostras feitas **antes** de o NTP responder (por exemplo, o Wi-Fi demorou a
subir) recebem o horário certo assim que o relógio sincroniza. Até lá elas esperam no
buffer — a proteção não espera nada disso.

## Limite conhecido

O STALE é calculado com `agora (backend) − ts_origem (nó)`. Se os relógios divergirem,
esse cálculo erra pela mesma diferença. Com NTP dos dois lados a diferença é de
milissegundos, muito abaixo do limite de 5 s.
