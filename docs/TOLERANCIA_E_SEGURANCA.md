# Tolerância a falhas e segurança

Esta etapa faz o sistema **reagir quando os dados param de chegar** e protege a comunicação.
O fluxo é o mesmo da entrega anterior:

```
nó de medição ──MQTT──▶ broker ──MQTT──▶ supervisório ──▶ SQLite ──▶ dashboard
```

## 1. Quando os dados ficam desatualizados

**Tempo máximo sem dados: 5 segundos.** O nó envia uma leitura **a cada 1 s**, então 5 s
são **5 leituras seguidas perdidas**. Com menos, por exemplo 2 s, uma oscilação normal de
Wi-Fi já acenderia o aviso. Com mais, por exemplo 30 s, o operador ficaria meio minuto
olhando um número velho achando que é atual. O valor fica em `STALE_S` e pode ser ajustado.

**O critério usa a hora da medição** (`ts_origem`), e não a hora em que o dado chegou:

```
desatualizado  ⇔  agora − ts_origem da última leitura > 5 s
```

Por isso uma leitura antiga que chega atrasada, vinda do buffer, **continua sendo tratada
como antiga**, e um dado velho nunca aparece como atual.

**O que a tela mostra quando passa de 5 s:**
- banner vermelho: "⚠ Comunicação perdida há **X s** — último dado recebido às **HH:MM:SS**";
- a causa provável: supervisório sem broker, nó desconectado (LWT) ou sem leitura nova;
- **o último valor continua na tela**, mas em cinza e marcado como "VALOR ANTIGO";
- a faixa "sem dados" no gráfico e "parado há X s" na faixa de dispositivos.

## 2. Teste: o que acontece quando os dados param

Dois tipos de falha foram testados, e nos dois o sistema volta **sozinho**, sem reiniciar
nada (reconexão automática no nó e no supervisório):

| Falha simulada | Como | O que o sistema faz |
|---|---|---|
| **Comunicação** | broker MQTT desligado | supervisório perde o broker; em 5 s o dado fica desatualizado; o nó guarda as leituras no buffer; na volta, reenvia tudo e o histórico se reconstrói |
| **Origem** | simulador (ou ESP32) desligado | em 5 s o dado fica desatualizado; o broker publica a última vontade (LWT) e o nó aparece *offline*; na volta, o nó reconecta e os dados voltam |

**Log de quando os dados pararam e voltaram.** O supervisório grava em
`logs/supervisorio.log` e na tabela `eventos` do banco, que aparece no dashboard:

```
2026-10-26 15:23:32 [SEM DADOS] moinho01: dados pararam de chegar; ultimo dado medido em 2026-10-26T18:23:27.873Z (ha 5.1s)
2026-10-26 15:23:54 [DADOS VOLTARAM] moinho01: dado atual de novo (26 s sem dados); lacunas restantes: 0
```

O teste automatizado `scripts/teste_falha_local.ps1` faz as duas falhas e confere tudo:
25 verificações aprovadas em 26/10/2026
(`evidencias/teste-falha-seguranca-2026-10-26/resultado.txt`).

## 3. Proteções contra falhas implementadas

O projeto implementa as quatro medidas que a atividade sugere:

| Medida | Onde | Por que faz sentido aqui |
|---|---|---|
| **Funcionamento local sem rede** | nó (ESP32/simulador) | é a principal: a proteção do moinho (desarme por sobrecorrente ou falha de sensor) é decidida no nó, sem rede. Uma queda de Wi-Fi não pode deixar o motor sem proteção |
| **Armazenamento temporário (buffer)** | nó, 300 leituras = 5 min | quedas de Wi-Fi na fábrica costumam ser curtas; o histórico não fica com buraco |
| **Reconexão automática** | nó e supervisório (tentativa a cada 1–5 s) | ninguém precisa reiniciar nada quando a rede volta |
| **Identificação de mensagens repetidas** | banco: chave única `(device_id, seq)` | o QoS 1 e o reenvio do buffer podem entregar a mesma leitura duas vezes; ela é descartada |

## 4. Segurança da comunicação

### Onde o fluxo pode ser atacado

| Trecho | Risco | Situação antes |
|---|---|---|
| **Nó → broker (Wi-Fi + MQTT, porta 1883)** | qualquer um na rede podia **se conectar ao broker sem senha**, ler todas as leituras e **publicar leituras falsas** como se fosse o `moinho01` (por exemplo, "corrente normal" durante uma sobrecorrente real) | broker aceitava conexão anônima |
| **Celular → API (`POST /api/gateway/...`)** | qualquer um na rede podia enviar leituras falsas por HTTP | rota aberta |
| **Tráfego em texto puro** | MQTT e HTTP sem criptografia: quem captura a rede (ex.: Wireshark) **lê** os dados e as senhas | sem TLS/HTTPS |
| **Senhas no código** | a senha do Wi-Fi ficava no `config.h`, que vai para o GitHub | arquivo versionado |

O trecho **mais crítico é o nó → broker**: uma leitura falsa ali chega ao supervisório e à
tela como verdadeira. Ela não desliga nem liga o moinho, porque a proteção é local, mas
engana o operador.

### O que já existia

- **Validação do conteúdo:** o supervisório recusa mensagem fora do formato (`schema`),
  com `device_id` diferente do tópico ou com valores fora do domínio.
- **A proteção não depende da rede:** um ataque à comunicação não consegue impedir o
  desarme local.

### O que foi implementado nesta etapa

1. **Usuário e senha no broker:** acabou a conexão anônima. Cada cliente tem o seu usuário:
   `no-moinho01` para o nó e `supervisorio` para o backend. O broker guarda só o **hash**
   da senha (Argon2 no broker de teste, PBKDF2 no Mosquitto).
2. **Permissão por tópico (ACL):** o `no-moinho01` só consegue publicar em
   `fabrica/planta1/moinho01/#`. Mesmo com a senha do nó, não dá para se passar por outro
   equipamento. Arquivos: `mosquitto/acl` e `mosquitto/amqtt-local.yaml`.
3. **Token na API do celular:** `POST /api/gateway/...` exige o cabeçalho
   `Authorization: Bearer <token>`. Sem token ou com token errado, a resposta é **401**, e a
   tentativa vai para o log como `[SEGURANCA] ... RECUSADO`. O token é comparado em tempo
   constante (`hmac.compare_digest`).
4. **Senhas fora do código** (ver abaixo).

Verificado no teste: conexão MQTT sem senha recusada ("Not authorized"); `no-moinho01`
publicando como `moinho99` foi bloqueado; API sem chave e com chave errada responderam 401;
com a chave certa, 200.

### O que ainda falta para produção (e como seria)

| Proteção | Para quê | Como |
|---|---|---|
| **TLS no MQTT** (porta 8883) | criptografar nó ↔ broker; hoje a senha trafega em texto puro | `listener 8883` + certificado no Mosquitto; `WiFiClientSecure` no ESP32 com o certificado da CA |
| **HTTPS** na API e no dashboard | criptografar celular/navegador ↔ supervisório | proxy reverso (Nginx/Caddy) com certificado na frente do FastAPI |
| **Login no dashboard (JWT)** | hoje qualquer um na rede **vê** os dados (leitura aberta) | rota de login que emite um JWT com validade curta; as rotas `/api/*` exigem o token |
| **Rede separada** | isolar a automação da rede corporativa | VLAN ou SSID próprio, com WPA2/WPA3, para os nós |
| **Troca periódica de senhas** | limitar o estrago de um vazamento | gerar outro `.env` e reiniciar |

TLS e HTTPS ficaram de fora para manter a demonstração simples (exigem gerar e instalar
certificados). Com eles, o mesmo usuário/senha e o mesmo token passam a viajar criptografados.

### Onde ficam as senhas e chaves

| Segredo | Fica em | No Git? |
|---|---|---|
| senhas do broker e token da API | arquivo **`.env`** na raiz do projeto, gerado com valores aleatórios por `scripts/preparar_segredos.py` | **não** (`.gitignore`); vai só o modelo `.env.exemplo` |
| hashes das senhas para o broker de teste | `mosquitto/senhas-amqtt.txt`, gerado a partir do `.env` | **não** |
| senha do Wi-Fi e do broker no ESP32 | `firmware/moinho01/secrets.h` | **não**; vai só `secrets.exemplo.h` |
| token no celular | `localStorage` do navegador do celular, digitado uma vez | — |

**Como o código recebe as senhas:** por **variável de ambiente**
(`MQTT_USER`, `MQTT_PASS`, `GATEWAY_TOKEN`). O `rodar-local.ps1` lê o `.env` e passa a cada
processo só a senha dele; o `docker-compose.yml` faz o mesmo com `${...}`. Nenhuma senha
está escrita no código. O teste automatizado confere que `.env`, `senhas-amqtt.txt` e
`secrets.h` **não estão versionados**.

**Se uma senha vazar:** gerar um `.env` novo (apagar o atual e rodar
`scripts/preparar_segredos.py`), atualizar o `secrets.h` do ESP32 e reiniciar.

## 5. Comportamento durante uma falha

O sistema tem **uma ação automática: o desarme do moinho** (abrir o relé/contator) por
sobrecorrente ou por falha de sensor.

| Situação | O que acontece com o desarme | Por quê |
|---|---|---|
| Wi-Fi ou broker caem | **continua funcionando**: o nó mede a cada 100 ms e desarma sozinho | a decisão é local; não lê nada da rede |
| Supervisório ou banco caem | continua funcionando | idem; o supervisório só monitora |
| Sensor para de responder (leitura fora da faixa por 3 leituras) | **desarma** (motivo `falha_sensor`) | sem medição, o nó não sabe se há sobrecorrente; o seguro é parar |
| Botoeira de emergência | corta o relé **eletricamente**, sem software | funciona até com o ESP32 travado |
| Alguém publica uma leitura falsa | **não afeta o desarme** | o nó não recebe comandos pela rede; a leitura falsa só chega à tela, e a ACL e a senha já a bloqueiam |

**Nenhuma decisão automática usa dado da rede.** O desarme usa só a leitura local, feita
naquele instante. Assim, nunca há decisão baseada em dado desatualizado ou em comunicação
indisponível. O que depende da rede é só **o que o operador vê**, e essa parte avisa
explicitamente quando o dado está velho. Um rearme pela tela exigiria rede, e por isso
o rearme é **físico** (botão no nó).
