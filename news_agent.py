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
]

MAX_AGE_HOURS = 6          # ignora notícias mais velhas que isso
MAX_ITEMS_TO_CLAUDE = 20   # teto de headlines por ciclo
MAX_BULLETS = 3            # teto de bullets na mensagem final
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

PROMPT = """Você é um analista de mercado sênior. Abaixo está uma lista de headlines recentes (JSON).

Selecione APENAS as notícias com potencial real de mover mercados — Ibovespa, BRL, curva de juros (DI), ou mercados globais (Fed, ECB, commodities, geopolítica com impacto econômico). Ignore ruído: fofoca corporativa menor, variação diária trivial, conteúdo repetido (se duas headlines cobrem o mesmo fato, escolha a melhor fonte).

Para cada selecionada, escreva um resumo de NO MÁXIMO 12 palavras, em PT-BR, direto ao ponto.

Selecione no máximo {max_bullets} itens. Se nada for relevante, retorne lista vazia.

Responda SOMENTE com JSON válido, sem markdown, neste formato:
{{"items": [{{"id": "...", "summary": "...", "emoji": "🇧🇷 ou 🌍 conforme o impacto principal"}}]}}

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
        if art:
            result.append({**art, "summary": item["summary"], "emoji": item.get("emoji", "📌")})
    return result


# ---------------------------------------------------------------
# 4. Mensagem + envio
# ---------------------------------------------------------------

def build_message(items: list[dict]) -> str:
    now = datetime.now(timezone.utc) - timedelta(hours=3)  # BRT
    lines = [f"📰 *Radar de Mercado* — {now.strftime('%d/%m %Hh%M')}", ""]
    for it in items:
        lines.append(f"{it['emoji']} {it['summary']} _({it['source']})_")
        lines.append(it["link"])
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
