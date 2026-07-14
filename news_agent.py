"""
Radar de Mercado — agente de notícias para WhatsApp
Fluxo: RSS -> dedupe (id + intra-day) -> Claude seleciona -> busca corpo real
(trafilatura) -> Claude redige por artigo -> Evolution API (grupo WhatsApp)
"""

import json
import os
import re
import hashlib
import requests
import feedparser
import trafilatura
from trafilatura.settings import use_config
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
MAX_ITEMS_TO_CLAUDE = 40   # teto de headlines por ciclo
MAX_NEWS = 5               # teto de NOTÍCIAS na mensagem final
MAX_BODY_CHARS = 6000      # teto do texto extraído por artigo (controla custo/tokens)
SENT_IDS_FILE = "sent_ids.json"
SENT_TODAY_FILE = "sent_today.json"   # memória intra-day (dedupe semântico)
MAX_SENT_TODAY = 100       # cap defensivo (um dia real fica bem abaixo disso)

# timeout curto no download do corpo — o job do Actions não pode travar num feed lento
_TRAFILATURA_CFG = use_config()
_TRAFILATURA_CFG.set("DEFAULT", "DOWNLOAD_TIMEOUT", "20")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

EVOLUTION_BASE_URL = os.environ["EVOLUTION_BASE_URL"].rstrip("/")   # ex: https://minha-evolution.up.railway.app
EVOLUTION_API_KEY = os.environ["EVOLUTION_API_KEY"]
EVOLUTION_INSTANCE = os.environ["EVOLUTION_INSTANCE"]               # nome da instância criada na Evolution
WHATSAPP_GROUP_JID = os.environ["WHATSAPP_GROUP_JID"]               # ex: 120363123456789012@g.us


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def now_brt() -> datetime:
    """Horário atual em BRT (UTC-3 fixo; o Brasil não tem mais horário de verão)."""
    return datetime.now(timezone.utc) - timedelta(hours=3)


# ---------------------------------------------------------------
# 1. Coleta
# ---------------------------------------------------------------

def article_id(entry) -> str:
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def clean_summary(entry) -> str:
    raw = entry.get("summary", "") or ""
    text = re.sub(r"<[^>]+>", "", raw)          # remove tags HTML
    text = re.sub(r"\s+", " ", text).strip()     # normaliza espaços
    return text[:400]                            # teto de tamanho


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
                    "summary": clean_summary(e),
                    "link": e.get("link", ""),
                    "source": source,
                })
        except Exception as ex:
            print(f"[warn] feed falhou {url}: {ex}")
    return articles


# ---------------------------------------------------------------
# 1b. Corpo real do artigo (trafilatura)
# ---------------------------------------------------------------

def fetch_article_body(link: str) -> str | None:
    """Baixa a página e extrai o texto principal. Devolve None se falhar
    (paywall, bloqueio, timeout) — o chamador cai de volta pro summary do RSS."""
    if not link:
        return None
    try:
        downloaded = trafilatura.fetch_url(link, config=_TRAFILATURA_CFG)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            config=_TRAFILATURA_CFG,
        )
        if not text:
            return None
        return text.strip()[:MAX_BODY_CHARS]
    except Exception as ex:
        print(f"[warn] extração de corpo falhou {link}: {ex}")
        return None


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
# 2b. Memória intra-day (dedupe semântico)
# ---------------------------------------------------------------

def load_sent_today() -> list[dict]:
    """Manchetes já enviadas HOJE (BRT). Zera sozinha à meia-noite BRT:
    se a data gravada for de outro dia, devolve lista vazia (reset preguiçoso)."""
    today = now_brt().strftime("%Y-%m-%d")
    if os.path.exists(SENT_TODAY_FILE):
        try:
            with open(SENT_TODAY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return data.get("headlines", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []   # dia novo, arquivo ausente ou corrompido -> memória vazia


def save_sent_today(headlines: list[dict]):
    with open(SENT_TODAY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"date": now_brt().strftime("%Y-%m-%d"),
             "headlines": headlines[-MAX_SENT_TODAY:]},
            f, ensure_ascii=False, indent=2,
        )


def format_sent_today(headlines: list[dict]) -> str:
    if not headlines:
        return "(nada enviado ainda hoje)"
    return "\n".join(f"- [{h.get('time', '')}] {h['headline']}" for h in headlines)


# ---------------------------------------------------------------
# 3. Filtro + redação com Claude (duas passadas)
# ---------------------------------------------------------------

def call_claude(prompt: str, max_tokens: int) -> str:
    """Uma chamada ao Haiku. Devolve o texto já sem cercas de markdown."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]
    return text.replace("```json", "").replace("```", "").strip()


def parse_claude_json(text: str):
    """Extrai o primeiro objeto/array JSON da resposta, ignorando prosa em volta.
    Haiku às vezes 'explica' fora do JSON — raw_decode lê só o primeiro valor
    válido e descarta o 'Extra data' que vem depois."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("nenhum JSON encontrado na resposta", text, 0)


# --- Passada 1: seleção (título + summary) -----------------------

SELECT_PROMPT = """# CONTEXT
Você faz a curadoria de um digest de notícias para um grupo de assessores de
investimento (financial advisors) brasileiros. Dominam o vocabulário de mercado —
Selic, DI, basis, carry, duration — e não precisam de explicação didática. O que
importa é o que pode virar pergunta de cliente ou exigir reposicionamento de
carteira.

Abaixo, notícias recentes em JSON, cada uma com "title" e "summary" (trecho curto
do RSS). Sua tarefa é APENAS SELECIONAR — não escreva manchete nem bullets agora.

# OBJECTIVE
Escolher as notícias com potencial real de mover mercados que o advisor acompanha.

Priorize, nesta ordem:
1. Política monetária e fiscal (Copom/BCB, Fed, ECB, Tesouro, arcabouço)
2. Política doméstica COM transmissão a mercado: votações e reformas no Congresso
   (tributária, fiscal, corte de gastos, meta), articulação do Executivo em torno
   do fiscal, estabilidade do governo, trocas em ministérios econômicos (Fazenda,
   Planejamento, BC), eleições e pesquisas que reprecificam risco fiscal ou câmbio
3. Indicadores que reprecificam curva ou câmbio (IPCA, payroll, CPI, PIB, Focus)
4. Crédito/risco sistêmico e movimentos setoriais amplos (não notícia de empresa
   isolada, salvo se mover índice ou setor inteiro)
5. Geopolítica e commodities com transmissão direta para ativos brasileiros

Ignore: variação diária trivial de ativo, fofoca corporativa, matéria de opinião,
conteúdo repetido (se duas cobrem o mesmo fato, escolha a fonte mais forte), e
qualquer coisa sem consequência clara para alocação. Escopo geográfico: só o que
afeta o mercado brasileiro ou os bancos centrais que transmitem para o Brasil.

Sobre política, a linha divisória é SEMPRE a transmissão a mercado: entra o fato
político que move fiscal, curva ou câmbio (voto de reforma, risco à meta, crise
que abre o risco-país). Fica de fora a fofoca partidária, o embate pessoal e o
escândalo sem efeito fiscal ou de mercado.

# JÁ ENVIADO HOJE
As manchetes abaixo já foram enviadas ao grupo HOJE. NÃO selecione uma notícia
cujo fato central já esteja nesta lista, mesmo que venha de outra fonte ou outro
link — o grupo já viu. ÚNICA exceção: desdobramento com informação materialmente
nova sobre o mesmo tema (ata divulgada após a decisão, novo número, revisão,
reversão). Na dúvida entre repetição e fato novo, corte.

{sent_today}

# RESPONSE
- No máximo {max_news} notícias. É melhor 2 fortes que 5 fracas. Se só houver 1
  relevante, selecione 1. Se nenhuma for relevante, retorne lista vazia.
- Se duas ou mais notícias cobrem o MESMO fato (ex.: a mesma alta do petróleo
  relatada por fontes diferentes), selecione APENAS UMA — a de fonte mais forte ou
  mais detalhada. Nunca encha o limite com variações do mesmo evento.
- Quando houver mais candidatas que o limite, corte primeiro as de menor impacto
  direto em preço de ativo brasileiro.
- Ordene do MAIS para o MENOS relevante.
- Responda SOMENTE com JSON válido, sem markdown, neste formato exato:
{{"ids": ["...", "..."]}}

Notícias:
{headlines}"""


def select_with_claude(articles: list[dict], sent_today: list[dict]) -> list[str]:
    headlines = [
        {"id": a["id"], "title": a["title"], "summary": a["summary"], "source": a["source"]}
        for a in articles
    ]
    text = call_claude(
        SELECT_PROMPT.format(
            max_news=MAX_NEWS,
            sent_today=format_sent_today(sent_today),
            headlines=json.dumps(headlines, ensure_ascii=False),
        ),
        max_tokens=500,
    )
    ids = parse_claude_json(text).get("ids", [])
    valid = {a["id"] for a in articles}
    # preserva a ordem de relevância, remove duplicatas e ids inválidos, aplica teto
    seen, out = set(), []
    for i in ids:
        if i in valid and i not in seen:
            seen.add(i)
            out.append(i)
    return out[:MAX_NEWS]


# --- Passada 2: redação (uma chamada por artigo, com o texto real) -----

WRITE_PROMPT = """# CONTEXT
Você escreve um item de digest para assessores de investimento brasileiros. São
pares técnicos — dominam Selic, DI, carry, duration. Nada de explicação didática.

Recebe UMA notícia já selecionada como relevante: o título e o TEXTO da matéria.
Use o texto como fonte dos detalhes.

# IDIOMA (crítico)
O texto da matéria pode vir em INGLÊS (fontes como MarketWatch, BBC, CNBC). Escreva
SEMPRE em português brasileiro fluente e idiomático — como um head de mesa brasileiro
escreveria, não como uma tradução. NUNCA traduza ao pé da letra nem preserve a ordem
de palavras ou as construções do inglês. Traduza o jargão para o termo usual do
mercado brasileiro (ex.: "front-month" -> "primeiro vencimento"; "two-day gain" ->
"alta em dois dias"). Se uma frase soaria estranha para um brasileiro, reescreva.

# OBJECTIVE
Escrever uma MANCHETE curta e de 2 a 3 BULLETS de detalhe, extraídos do texto.

# STYLE
Telegráfico e denso. A manchete resume o fato central em poucas palavras. Cada
bullet extrai um dado concreto do texto: número, declaração, valor, o que foi
decidido. Se o texto não trouxer detalhe suficiente para 2 bullets factuais,
escreva menos bullets — nunca preencha com paráfrase da manchete nem com contexto
que você presume. Sem introdução, sem "segundo a matéria", sem adjetivo
desnecessário.

# TONE
Objetivo, profissional, seco. Como um head de mesa manda no grupo interno. Nunca
alarmista, nunca promocional.

# ANTI-INFERÊNCIA (regra mais importante)
Leitura ou reação de mercado (mercado já precificava, curva abriu, ativo caiu)
SOMENTE se o texto a afirmar. Você NUNCA infere direção de preço nem reação por
conta própria. Na dúvida, reporte só o fato. Não dê recomendação nem opinião.

# RESPONSE
- "headline": manchete curta, no máximo 10 palavras, em PT-BR.
- "bullets": 2 a 3 itens, cada um no máximo 20 palavras, em PT-BR, extraídos do
  texto.
- Responda SOMENTE com JSON válido, sem markdown, neste formato exato:
{{"headline": "...", "bullets": ["...", "..."]}}

Notícia:
Título: {title}
Fonte: {source}
Texto:
{body}"""


def write_item(article: dict) -> dict | None:
    """Redige headline+bullets de UM artigo, com o corpo real (ou o summary
    como fallback). Devolve None se a resposta vier vazia ou inválida."""
    body = article.get("body") or article["summary"]
    text = call_claude(
        WRITE_PROMPT.format(
            title=article["title"],
            source=article["source"],
            body=body,
        ),
        max_tokens=600,
    )
    try:
        data = parse_claude_json(text)
    except json.JSONDecodeError:
        print(f"[warn] JSON inválido na redação de {article['id']}")
        return None
    headline = data.get("headline", "").strip()
    bullets = [b.strip() for b in data.get("bullets", []) if b.strip()]
    if not headline or not bullets:
        return None
    return {**article, "headline": headline, "bullets": bullets}


# ---------------------------------------------------------------
# 4. Mensagem + envio
# ---------------------------------------------------------------

def build_message(items: list[dict]) -> str:
    now = now_brt()
    lines = [f"*Radar de Mercado* — {now.strftime('%d/%m %Hh%M')}", ""]
    for it in items:
        lines.append(f"*{it['headline']}*")
        for b in it["bullets"]:
            b = b.rstrip()
            if not b.endswith((".", "!", "?")):
                b += "."
            lines.append(f"• {b}")
        lines.append(f"• Fonte: _{it['source']}_.")
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
    sent_today = load_sent_today()   # já zerado se for um novo dia BRT
    articles = [a for a in fetch_articles() if a["id"] not in sent]
    print(f"[info] {len(articles)} headlines novas | {len(sent_today)} já enviadas hoje")
    if not articles:
        return

    articles = articles[:MAX_ITEMS_TO_CLAUDE]

    # Passada 1: seleção por título + summary
    selected_ids = select_with_claude(articles, sent_today)
    by_id = {a["id"]: a for a in articles}
    selected = [by_id[i] for i in selected_ids]
    print(f"[info] {len(selected)} selecionadas pelo Claude")

    # Busca o corpo real de cada selecionada (fallback: summary do RSS)
    for art in selected:
        body = fetch_article_body(art.get("link", ""))
        if body:
            art["body"] = body
        else:
            print(f"[warn] sem corpo, usando summary: {art['source']}")

    # Passada 2: redação, uma chamada por artigo, com o texto em mãos
    items = [w for art in selected if (w := write_item(art))]
    print(f"[info] {len(items)} redigidas")

    if items:
        send_whatsapp(build_message(items))
        # registra na memória intra-day só o que foi realmente enviado
        hora = now_brt().strftime("%Hh%M")
        sent_today.extend({"time": hora, "headline": it["headline"]} for it in items)
        save_sent_today(sent_today)

    # marca TODAS as vistas (mesmo as descartadas) pra não reavaliar
    sent.update(a["id"] for a in articles)
    save_sent_ids(sent)


if __name__ == "__main__":
    main()
