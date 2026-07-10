# Radar de Mercado — WhatsApp News Agent

RSS → dedupe → Claude (filtro + resumo) → grupo WhatsApp via Evolution API. Roda de hora em hora via GitHub Actions, dias úteis, 07h–19h BRT.

## Formato da mensagem

```
📰 *Radar de Mercado* — 09/07 14h00

🇧🇷 Copom mantém Selic e endurece tom sobre fiscal (InfoMoney)
https://...

🌍 Fed sinaliza corte em setembro após CPI abaixo do esperado (CNBC)
https://...
```

## Setup (uma vez só)

### 1. Evolution API (o elo com o WhatsApp)

A Evolution API precisa rodar num servidor persistente (ela mantém a sessão do WhatsApp Web aberta). Este guia usa **Railway**.

**Atenção a dois pontos onde é fácil errar:**

- A imagem antiga `atendai/evolution-api` foi **descontinuada** e falha ao baixar. Use `evoapicloud/evolution-api`, com **versão fixa** (`:v2.3.0`) — nunca `:latest`.
- A v2 **exige PostgreSQL**. Sem banco, o container entra em loop de restart com `Error: Database provider invalid.` Não existe modo "sem banco" nessa versão.

Passos:

1. Railway → **New Project** → **Deploy from Docker Image** → `evoapicloud/evolution-api:v2.3.0`
2. Aba **Variables**, adicione:

   | Nome | Valor |
   |---|---|
   | `AUTHENTICATION_API_KEY` | invente uma senha longa |
   | `AUTHENTICATION_TYPE` | `apikey` |

3. No canvas do projeto → **+ Create** → **Database** → **Add PostgreSQL**. Espere ficar Online.
4. Volte às Variables do `evolution-api` e adicione:

   | Nome | Valor |
   |---|---|
   | `DATABASE_PROVIDER` | `postgresql` |
   | `DATABASE_CONNECTION_URI` | `${{Postgres.DATABASE_URL}}` (referência, não cole a string) |
   | `DATABASE_SAVE_DATA_INSTANCE` | `true` |

5. **Settings → Networking → Generate Domain**, porta **8080**.
6. Redeploy. Nos logs, procure as migrations do Prisma e o servidor subindo na 8080.
7. Abra a URL no navegador. Deve retornar um JSON com `"status":200`.

### 2. Conectar o WhatsApp

Use o **Manager**, a interface web embutida — mais simples que curl:

1. Abra `https://SUA-URL.up.railway.app/manager`
2. Login com a `AUTHENTICATION_API_KEY`
3. **Instance +** → Name: `Radar` → Channel: **Baileys** (não "WhatsApp Cloud API", que não envia para grupos) → Save
4. Clique no card da instância → **Connect** → escaneie o QR com o **chip dedicado** (o QR expira em ~40s)
5. Status deve virar `open` / Connected
6. Pelo seu celular pessoal, **adicione o número do bot ao grupo** de destino

> O nome da instância é **case-sensitive**. Se você criou como `Radar`, o secret tem que ser `Radar` — não `radar`.

### 3. Descobrir o JID do grupo

```bash
curl -H "apikey: SUA_CHAVE" \
  "https://SUA-URL.up.railway.app/group/fetchAllGroups/Radar?getParticipants=false"
```

Ache o grupo pelo campo `subject` e copie o `id` — formato `120363123456789012@g.us`.

Teste o envio antes de seguir:

```bash
curl -X POST "https://SUA-URL.up.railway.app/message/sendText/Radar" \
  -H "apikey: SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -d '{"number": "120363123456789012@g.us", "text": "teste do radar"}'
```

Se a mensagem chegar no grupo, a parte difícil acabou.

### 4. Repositório GitHub

1. Crie um repo privado e suba estes arquivos. O workflow vai em `.github/workflows/news.yml`.
2. Em **Settings → Secrets and variables → Actions**, adicione:

| Secret | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | sua chave da API Anthropic (console.anthropic.com) |
| `EVOLUTION_BASE_URL` | `https://...up.railway.app` (sem barra no final) |
| `EVOLUTION_API_KEY` | a `AUTHENTICATION_API_KEY` do Railway |
| `EVOLUTION_INSTANCE` | `Radar` (exatamente como criado, case-sensitive) |
| `WHATSAPP_GROUP_JID` | `120363123456789012@g.us` |

3. Aba **Actions** → habilite workflows se pedir → **Radar de Mercado** → **Run workflow**.

### 5. Ajustes

- **Feeds**: edite a lista `FEEDS` no topo do `news_agent.py`. Valide cada URL antes (feeds mudam). Bons candidatos extras: Valor, Bloomberg Línea, feeds do BCB.
- **Frequência**: mude o `cron` no workflow.
- **Rigor do filtro**: ajuste o `PROMPT` — dá pra pedir só "alto impacto", incluir cripto, etc.
- **Custo**: com `claude-haiku-4-5` e ~13 execuções/dia, o custo da API é de centavos por dia.

## Avisos

- Evolution API usa sessão de WhatsApp Web — **contra os ToS da Meta**. Use um chip dedicado e aceite o risco de ban desse número.
- O `sent_ids.json` é commitado de volta pelo workflow para o dedupe persistir entre execuções.
