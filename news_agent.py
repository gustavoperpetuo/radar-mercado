"""
Radar de Mercado — agente de notícias para WhatsApp
Fluxo: RSS -> dedupe -> Claude (filtro + resumo) -> Evolution API (grupo WhatsApp)
"""

import json
import os
import hashlib
import requests
import feedparser
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------

FEEDS = [
    # Brasil — valide/ajuste as URLs, feeds mudam de tempos em tempos
    "https://www.infomoney.com.br/feed/",
    "https://g1.globo.com/rss/g1/economia/",
    "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml",
    # Global
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
    "https://www.reuters.com/",
]

MAX_AGE_HOURS = 6          # ignora notícias mais velhas que isso
MAX_ITEMS_TO_CLAUDE = 40   # teto de headlines por ciclo
MAX_BULLETS = 5            # teto de NOTÍCIAS na mensagem final
SENT_IDS_FILE = "sent_ids.json"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

EVOLUTION_BASE_URL = os.environ["EVOLUTION_BASE_URL"].rstrip("/")   # ex: https://minha-evolution.up.railway.app
EVOLUTION_API_KEY = os.environ["EVOLUTION_API_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]               # nome da instância criada na Evolution
WHATSAPP_GROUP_JID = os.environ["WHATSAPP_GROUP_JID"]               # ex: 120363123456789012@g.us


# ---------------------------------------------------------------
# 1. Coleta
# ---------------------------------------------------------------

def article_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fetch_articles() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    articles = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for e in feed.entries:
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    if e.get(key):
                        published = datetime(*e[key][:6], tzinfo=timezone.utc)
                        break
                if published and published < cutoff:
                    continue
                articles.append({
                    "id": article_id(e),
                    "title": e.get("title", "").strip(),
                    "link": e.get("link", ""),
                    "source": source,
                })
        except Exception as ex:
            print(f"[warn] feed falhou {url}: {ex}")
    return articles


# ---------------------------------------------------------------
# 2. Dedupe
# ---------------------------------------------------------------

def load_sent_ids() -> set:
    if os.path.exists(SENT_IDS_FILE):
        with open(SENT_IDS_FILE) as f:
            return set(json.load(f))
    return set()


def save_sent_ids(ids: set):
    # mantém só os 2000 mais recentes pra não crescer infinito
    with open(SENT_IDS_FILE, "w") as f:
        json.dump(list(ids)[-2000:], f)


# ---------------------------------------------------------------
# 3. Filtro + resumo com Claude
# ---------------------------------------------------------------

PROMPT = """# CONTEXT
Você monta um digest de notícias para um grupo de assessores de investimento
(financial advisors) brasileiros. Eles recebem esta mensagem no WhatsApp algumas
vezes ao dia. Dominam o vocabulário de mercado — Selic, DI, basis, carry, duration
— e não precisam de explicações didáticas. O que eles precisam é saber, rápido, o
que aconteceu que pode virar pergunta de cliente ou exigir reposicionamento de
carteira.

Abaixo, uma lista de headlines recentes em JSON.

# OBJECTIVE
Selecionar as notícias com potencial real de mover mercados que o advisor
acompanha. Para cada uma, escrever uma MANCHETE curta e de 2 a 3 BULLETS de
detalhe.

Priorize, nesta ordem:
1. Política monetária e fiscal (Copom/BCB, Fed, ECB, Tesouro, arcabouço)
2. Indicadores que reprecificam curva ou câmbio (IPCA, payroll, CPI, PIB, Focus)
3. Crédito/risco sistêmico e movimentos setoriais amplos (não notícia de empresa
   isolada, salvo se mover índice ou setor inteiro)
4. Geopolítica e commodities com transmissão direta para ativos brasileiros

Ignore: variação diária trivial de ativo, fofoca corporativa, matéria de opinião,
conteúdo repetido (se duas cobrem o mesmo fato, escolha a fonte mais forte), e
qualquer coisa sem consequência clara para alocação.

# STYLE
Telegráfico e denso. A manchete resume o fato central em poucas palavras. Cada
bullet acrescenta um dado concreto: número, declaração, o que foi decidido. Sem
introdução, sem "segundo a matéria", sem adjetivo desnecessário.

# TONE
Objetivo, profissional, seco. Como um head de mesa manda no grupo interno. Nunca
alarmista, nunca promocional.

# AUDIENCE
Assessores de investimento experientes. Trate-os como pares técnicos.

# RESPONSE
Regras invioláveis:
- No máximo {max_bullets} notícias. Se nada for relevante, retorne lista vazia.
- "headline": manchete curta, no máximo 10 palavras, em PT-BR.
- "bullets": lista de 2 a 3 itens, cada um no máximo 20 palavras, em PT-BR.
- Leitura ou reação de mercado (mercado já precificava, curva abriu, ativo caiu)
  SOMENTE se a headline/fonte o afirmar. Você NÃO infere direção de preço nem
  reação por conta própria. Bullet que não vem da fonte reporta apenas o fato.
- Não dê recomendação de investimento nem opinião sua.
- Responda SOMENTE com JSON válido, sem markdown, neste formato exato:
{{"items": [{{"id": "...", "headline": "...", "bullets": ["...", "..."]}}]}}

Headlines:
{headlines}"""


def filter_with_claude(articles: list[dict]) -> list[dict]:
    headlines = [{"id": a["id"], "title": a["title"], "source": a["source"]} for a in articles]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1500,
            "messages": [{
                "role": "user",
                "content": PROMPT.format(
                    max_bullets=MAX_BULLETS,
                    headlines=json.dumps(headlines, ensure_ascii=False),
                ),
            }],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    text = text.replace("```json", "").replace("```", "").strip()
    selected = json.loads(text)["items"]

    by_id = {a["id"]: a for a in articles}
    result = []
    for item in selected:
        art = by_id.get(item["id"])
        if not art:
            continue
        headline = item.get("headline", "").strip()
        bullets = [b.strip() for b in item.get("bullets", []) if b.strip()]
        if not headline or not bullets:
            continue
        result.append({**art, "headline": headline, "bullets": bullets})
    return result


# ---------------------------------------------------------------
# 4. Mensagem + envio
# ---------------------------------------------------------------

def build_message(items: list[dict]) -> str:
    now = datetime.now(timezone.utc) - timedelta(hours=3)  # BRT
    lines = [f"*Radar de Mercado* — {now.strftime('%d/%m %Hh%M')}", ""]
    for it in items:
        lines.append(f"*{it['headline']}*")
        for b in it["bullets"]:
            lines.append(f"• {b}")
        lines.append(f"• Fonte: _{it['source']}_")
        lines.append("")
    return "\n".join(lines).strip()


def send_whatsapp(text: str):
    resp = requests.post(
        f"{EVOLUTION_BASE_URL}/message/sendText/{EVOLUTION_INSTANCE}",
        headers={"apikey": EVOLUTION_API_KEY, "content-type": "application/json"},
        json={"number": WHATSAPP_GROUP_JID, "text": text},
        timeout=60,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Instância '{EVOLUTION_INSTANCE}' não encontrada (404). "
            "O nome é case-sensitive — confira no Manager da Evolution "
            "e ajuste o secret EVOLUTION_INSTANCE."
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Autenticação recusada pela Evolution. Confira o secret EVOLUTION_API_KEY "
            "e certifique-se de que EVOLUTION_BASE_URL não tem barra no final."
        )
    resp.raise_for_status()
    print("[ok] mensagem enviada")


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

def main():
    sent = load_sent_ids()
    articles = [a for a in fetch_articles() if a["id"] not in sent]
    print(f"[info] {len(articles)} headlines novas")
    if not articles:
        return

    articles = articles[:MAX_ITEMS_TO_CLAUDE]
    selected = filter_with_claude(articles)
    print(f"[info] {len(selected)} selecionadas pelo Claude")

    if selected:
        send_whatsapp(build_message(selected))

    # marca TODAS as vistas (mesmo as descartadas) pra não reavaliar
    sent.update(a["id"] for a in articles)
    save_sent_ids(sent)


if __name__ == "__main__":
    main()
