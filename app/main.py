import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote_plus, unquote

import duckdb
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earnings-app")

# Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.86.35:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))
DB_PATH = os.getenv("DATABASE_PATH", "/app/data/earnings.db")
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()

HF_TRANSCRIPTS_PARQUET = (
    "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/"
    "resolve/main/data/US/stock_earning_call_transcripts.parquet"
)
HF_TICKERS_JSON = (
    "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/"
    "resolve/main/data/US/company_tickers.json"
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Built-in S&P 500 fallback catalog
FALLBACK_SP500 = {"SPCX":"Space Exploration Technologies Corp.","ASML":"ASML Holding",
    "ARM":"Arm Holdings","SHOP":"Shopify Inc.","PDD":"PDD Holdings Inc.","MELI":"Mercado Libre, Inc",
    "MSTR":"Strategy Inc","":"Coca-Cola Europacific Partners","ALAB":"Astera Labs, Inc.","TRI":"Thomson Reuters Corporation",
    "NBIS":"Nebius Group","FER":"Ferrovial","CRWV":"CoreWeave, Inc.","RKLB":"Rocket Lab Corporation","ALNY":"Alnylam Pharmaceuticals, Inc.",
    "AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation", "NVDA": "NVIDIA Corporation",
    "AMZN": "Amazon.com, Inc.", "META": "Meta Platforms, Inc.", "GOOGL": "Alphabet Inc.",
    "TSLA": "Tesla, Inc.", "AVGO": "Broadcom Inc.", "BRK-B": "Berkshire Hathaway Inc.",
    "JPM": "JPMorgan Chase & Co.", "LLY": "Eli Lilly and Company", "V": "Visa Inc.",
    "UNH": "UnitedHealth Group", "XOM": "Exxon Mobil Corporation", "MA": "Mastercard Incorporated",
    "COST": "Costco Wholesale", "HD": "The Home Depot, Inc.", "PG": "Procter & Gamble",
    "NFLX": "Netflix, Inc.", "AMD": "Advanced Micro Devices, Inc.", "ORCL": "Oracle Corporation",
    "CRM": "Salesforce, Inc.", "WMT": "Walmart Inc.", "BAC": "Bank of America",
    "KO": "The Coca-Cola Company", "PEP": "PepsiCo, Inc.", "INTC": "Intel Corporation",
    "CSCO": "Cisco Systems, Inc.", "QCOM": "QUALCOMM Incorporated", "TXN": "Texas Instruments",
    "AMAT": "Applied Materials, Inc.", "MU": "Micron Technology, Inc.", "NOW": "ServiceNow, Inc.",
    "GS": "The Goldman Sachs Group", "MS": "Morgan Stanley", "CAT": "Caterpillar Inc.",
    "IBM": "International Business Machines", "GE": "GE Aerospace", "ISRG": "Intuitive Surgical",
    "UBER": "Uber Technologies, Inc.", "PLTR": "Palantir Technologies Inc.", "PANW": "Palo Alto Networks",
    "RTX": "RTX Corporation", "COF": "Capital One Financial", "MSCI": "MSCI Inc.",
    "STLD": "Steel Dynamics, Inc.", "GPC": "Genuine Parts Company", "KEY": "KeyCorp",
    "HAS": "Hasbro, Inc.", "LMT": "Lockheed Martin", "NOC": "Northrop Grumman",
    "DE": "Deere & Company", "HON": "Honeywell International", "UNP": "Union Pacific",
    "BA": "The Boeing Company", "C": "Citigroup Inc.", "WFC": "Wells Fargo & Company",
    "AXP": "American Express", "BLK": "BlackRock, Inc.", "SCHW": "Charles Schwab",
    "T": "AT&T Inc.", "VZ": "Verizon Communications", "TMUS": "T-Mobile US",
    "DIS": "The Walt Disney Company", "CMCSA": "Comcast Corporation", "NKE": "NIKE, Inc.",
    "SBUX": "Starbucks Corporation", "MCD": "McDonald's Corporation", "LOW": "Lowe's Companies",
}

sync_status = {"running": False, "last_sync": None, "message": "Idle"}


# ---------------------------------------------------------------------------
# Database Layer (SQLite)
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sector TEXT DEFAULT '',
                is_sp500 INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                company_name TEXT NOT NULL,
                fiscal_year INTEGER,
                fiscal_quarter INTEGER,
                report_date TEXT,
                source TEXT,
                is_sp500 INTEGER DEFAULT 0,
                raw_text TEXT,
                segments_json TEXT,
                sentiment TEXT DEFAULT 'Neutral',
                snippet TEXT,
                summary_markdown TEXT,
                model_used TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON transcripts(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sp500_date ON transcripts(is_sp500, report_date DESC)")
        conn.commit()


# ---------------------------------------------------------------------------
# Company & S&P 500 Catalog Sync
# ---------------------------------------------------------------------------
async def seed_companies():
    sp500_map = dict(FALLBACK_SP500)

    # 1. Fetch full S&P 500 list from Wikipedia
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": BROWSER_UA}) as client:
            resp = await client.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                table = soup.find("table", {"id": "constituents"})
                if table:
                    for row in table.find_all("tr")[1:]:
                        cols = row.find_all(["td", "th"])
                        if len(cols) >= 2:
                            sym = cols[0].get_text(strip=True).upper()
                            name = cols[1].get_text(strip=True)
                            if sym:
                                sp500_map[sym] = name
                                sp500_map[sym.replace(".", "-")] = name
                logger.info(f"Loaded {len(sp500_map)} S&P 500 symbols from Wikipedia.")
    except Exception as e:
        logger.warning(f"Wikipedia S&P 500 fetch failed, using fallback list: {e}")

    # 2. Fetch SEC / US company ticker catalog
    all_companies = {}
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
        ) as client:
            resp = await client.get(HF_TICKERS_JSON)
            if resp.status_code == 200:
                records = resp.json()
                if isinstance(records, list):
                    for item in records:
                        sym = str(item.get("symbol") or item.get("ticker") or "").upper()
                        name = str(item.get("company_name") or item.get("title") or sym)
                        if sym:
                            all_companies[sym] = name
                elif isinstance(records, dict):
                    for _, item in records.items():
                        sym = str(item.get("ticker") or item.get("symbol") or "").upper()
                        name = str(item.get("title") or item.get("company_name") or sym)
                        if sym:
                            all_companies[sym] = name
    except Exception as e:
        logger.warning(f"US ticker catalog fetch skipped: {e}")

    with get_db() as conn:
        for sym, name in sp500_map.items():
            conn.execute(
                "INSERT OR REPLACE INTO companies (symbol, name, is_sp500) VALUES (?, ?, 1)",
                (sym.upper(), name),
            )
        for sym, name in all_companies.items():
            sym_u = sym.upper()
            if sym_u not in sp500_map:
                conn.execute(
                    "INSERT OR IGNORE INTO companies (symbol, name, is_sp500) VALUES (?, ?, 0)",
                    (sym_u, name),
                )
        conn.commit()


def get_sp500_set() -> set:
    with get_db() as conn:
        rows = conn.execute("SELECT symbol FROM companies WHERE is_sp500 = 1").fetchall()
    s = {r["symbol"].upper() for r in rows}
    return s if s else {k.upper() for k in FALLBACK_SP500.keys()}


def resolve_company(query_str: str) -> Optional[dict]:
    q = query_str.strip()
    if not q:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, name, is_sp500 FROM companies WHERE UPPER(symbol) = UPPER(?)",
            (q,),
        ).fetchone()
        if row:
            return dict(row)

        row = conn.execute(
            """
            SELECT symbol, name, is_sp500 FROM companies
            WHERE UPPER(symbol) LIKE UPPER(?) OR UPPER(name) LIKE UPPER(?)
            ORDER BY is_sp500 DESC, LENGTH(symbol) ASC
            LIMIT 1
            """,
            (f"{q}%", f"%{q}%"),
        ).fetchone()
        if row:
            return dict(row)

    if re.match(r"^[A-Za-z.\-]{1,6}$", q):
        return {"symbol": q.upper(), "name": q.upper(), "is_sp500": 0}
    return None


# ---------------------------------------------------------------------------
# Engine 1: The Motley Fool Live Transcripts (Feed + Quote Page + Search)
# ---------------------------------------------------------------------------
def parse_fool_url_metadata(url: str, link_text: str = "") -> dict:
    """Extracts symbol, quarter, year, and report_date from a Motley Fool transcript URL/title."""
    # Example URL: /earnings/call-transcripts/2026/10/20/ge-ge-q3-2026-earnings-call-transcript/
    date_match = re.search(r"/call-transcripts/(\d{4})/(\d{2})/(\d{2})/", url)
    report_date = (
        f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        if date_match
        else datetime.now().strftime("%Y-%m-%d")
    )

    q_match = re.search(r"-q([1-4])-(\d{4})-earnings", url, re.I) or re.search(
        r"Q([1-4])\s+(\d{4})", link_text, re.I
    )
    fq = int(q_match.group(1)) if q_match else ((datetime.now().month - 1) // 3 + 1)
    fy = int(q_match.group(2)) if q_match else datetime.now().year

    # Extract ticker from title like "GE (GE) Q3 2026" or slug like "...-ge-q3-2026-..."
    sym = None
    t_match = re.search(r"\(([A-Z.\-]{1,6})\)", link_text)
    if t_match:
        sym = t_match.group(1).upper()
    else:
        slug_match = re.search(r"/([a-z0-9\-]+)-q[1-4]-\d{4}-earnings", url, re.I)
        if slug_match:
            parts = slug_match.group(1).split("-")
            if parts:
                sym = parts[-1].upper()

    return {
        "symbol": sym,
        "fiscal_year": fy,
        "fiscal_quarter": fq,
        "report_date": report_date,
    }


def scrape_fool_listing_pages(max_pages: int = 5) -> List[dict]:
    """Walks https://www.fool.com/earnings-call-transcripts/ to discover recent transcripts."""
    discovered = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        url = f"https://www.fool.com/earnings-call-transcripts/?page={page}"
        try:
            resp = cf_requests.get(url, impersonate="chrome120", timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/earnings/call-transcripts/" not in href:
                    continue
                full_url = href if href.startswith("http") else f"https://www.fool.com{href}"
                if full_url in seen_urls:
                    continue
                text = a.get_text(" ", strip=True)
                meta = parse_fool_url_metadata(full_url, text)
                if meta["symbol"]:
                    seen_urls.add(full_url)
                    discovered.append({**meta, "url": full_url, "title": text})
        except Exception as e:
            logger.warning(f"Error scraping Motley Fool listing page {page}: {e}")

    return discovered


def parse_fool_transcript_article(url: str, fallback_symbol: str) -> Optional[dict]:
    """Downloads and parses a full Motley Fool earnings call transcript into speaker turns."""
    try:
        resp = cf_requests.get(url, impersonate="chrome120", timeout=20)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        h1 = soup.find("h1")
        title_text = h1.get_text(" ", strip=True) if h1 else ""
        meta = parse_fool_url_metadata(url, title_text)
        sym = fallback_symbol.upper() or meta["symbol"]

        article = (
            soup.find("div", {"class": re.compile(r"article-body|tailwind-article-body|content", re.I)})
            or soup.find("article")
            or soup.find("main")
            or soup
        )

        paragraphs = article.find_all(["p", "h2", "h3", "li"])
        segments = []
        lines = []
        current_speaker = "Transcript"
        in_transcript_body = False

        for p in paragraphs:
            txt = p.get_text(" ", strip=True)
            if not txt or len(txt) < 3:
                continue

            # Skip boilerplate footer/promo lines
            if any(
                skip in txt
                for skip in [
                    "Motley Fool Transcribing",
                    "Image source: The Motley Fool",
                    "Don't miss this second chance",
                    "Stock Advisor",
                    "has positions in",
                ]
            ):
                continue

            if "Full Conference Call Transcript" in txt or "Prepared Remarks" in txt:
                in_transcript_body = True
                continue

            # Detect standalone speaker header: e.g. "Larry Culp -- Chairman and CEO"
            strong = p.find("strong")
            if strong and len(txt) < 120 and ("--" in txt or "—" in txt or "-" in txt):
                current_speaker = strong.get_text(" ", strip=True).rstrip(":")
                in_transcript_body = True
                continue

            # Detect inline speaker: e.g. "Larry Culp: Thank you..." or "Operator: Good day..."
            inline_match = re.match(r"^([A-Z][A-Za-z0-9\s.\-',()]{1,45}):\s+(.+)$", txt)
            if inline_match:
                current_speaker = inline_match.group(1).strip()
                content = inline_match.group(2).strip()
                in_transcript_body = True
            else:
                if len(txt) < 25 and not in_transcript_body:
                    continue
                content = txt

            if len(content) >= 15:
                seg_idx = len(segments) + 1
                segments.append(
                    {
                        "paragraph_number": seg_idx,
                        "speaker": current_speaker,
                        "content": content,
                    }
                )
                lines.append(f"[{current_speaker}]: {content}")

        if len(segments) < 5:
            return None

        return {
            "symbol": sym,
            "fiscal_year": meta["fiscal_year"],
            "fiscal_quarter": meta["fiscal_quarter"],
            "report_date": meta["report_date"],
            "source": "The Motley Fool (Verbatim Transcript)",
            "segments": segments,
            "raw_text": "\n\n".join(lines),
        }
    except Exception as e:
        logger.warning(f"Failed parsing Motley Fool article {url}: {e}")
        return None


def find_fool_transcript_for_symbol(symbol: str) -> Optional[dict]:
    """Resolves the latest Motley Fool transcript for any ticker via Feed, Quote Page, or Search."""
    sym = symbol.upper()
    slug_sym = sym.lower().replace(".", "-")

    # 1. Check recent listing pages (fast path for newly reported earnings like GE)
    recent_items = scrape_fool_listing_pages(max_pages=3)
    for item in recent_items:
        if item["symbol"] == sym:
            parsed = parse_fool_transcript_article(item["url"], sym)
            if parsed:
                return parsed

    # 2. Check Motley Fool Quote pages (NYSE & NASDAQ)
    for exchange in ["nyse", "nasdaq", "nysemkt"]:
        quote_url = f"https://www.fool.com/quote/{exchange}/{slug_sym}/"
        try:
            resp = cf_requests.get(quote_url, impersonate="chrome120", timeout=12)
            if resp.status_code == 200 and "/earnings/call-transcripts/" in resp.text:
                matches = re.findall(
                    r'(?:https://www\.fool\.com)?(/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/)',
                    resp.text,
                    re.I,
                )
                for path in matches:
                    if f"-{slug_sym}-" in path.lower():
                        full_url = f"https://www.fool.com{path}"
                        parsed = parse_fool_transcript_article(full_url, sym)
                        if parsed:
                            return parsed
        except Exception:
            pass

    # 3. Search Engine Resolver (Yahoo Search / DuckDuckGo HTML) for older quarters
    search_queries = [
        f'site:fool.com/earnings/call-transcripts/ "({sym})" "Earnings Call Transcript"',
        f'site:fool.com/earnings/call-transcripts/ "{sym}" earnings call transcript',
    ]
    for q in search_queries:
        try:
            s_url = f"https://search.yahoo.com/search?p={quote_plus(q)}"
            resp = cf_requests.get(s_url, impersonate="chrome120", timeout=12)
            if resp.status_code == 200:
                raw_html = unquote(resp.text)
                urls = re.findall(
                    r"https://www\.fool\.com/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/",
                    raw_html,
                    re.I,
                )
                # Sort descending by date in URL path (/YYYY/MM/DD/)
                urls = sorted(set(urls), reverse=True)
                for candidate_url in urls:
                    if f"-{slug_sym}-" in candidate_url.lower():
                        parsed = parse_fool_transcript_article(candidate_url, sym)
                        if parsed:
                            return parsed
        except Exception as e:
            logger.warning(f"Search resolver failed for {sym}: {e}")

    return None


# ---------------------------------------------------------------------------
# Engine 2: Yahoo Finance Live Transcript Extractor (HTML + SvelteKit JSON + Search)
# ---------------------------------------------------------------------------
def fetch_transcript_from_yahoo_live(symbol: str) -> Optional[dict]:
    sym = symbol.upper()
    candidate_urls = []

    # 1. Inspect both finance.yahoo.com and stage.finance.yahoo.com earnings-calls page
    for host in ["https://finance.yahoo.com", "https://stage.finance.yahoo.com"]:
        list_url = f"{host}/quote/{sym}/earnings-calls/"
        try:
            resp = cf_requests.get(list_url, impersonate="chrome120", timeout=15)
            if resp.status_code == 200:
                matches = re.findall(
                    r'(?:https?://(?:stage\.)?finance\.yahoo\.com)?(/quote/[A-Za-z0-9.\-]+/earnings/[A-Za-z0-9.\-_]+earnings_call[A-Za-z0-9.\-_]*\.html)',
                    resp.text,
                    re.I,
                )
                for m in matches:
                    candidate_urls.append(f"https://finance.yahoo.com{m}")
        except Exception:
            pass

    # 2. Fallback: Yahoo Search index for direct Yahoo Finance transcript URLs
    if not candidate_urls:
        try:
            q = f'site:finance.yahoo.com/quote/{sym}/earnings/ "earnings_call"'
            s_resp = cf_requests.get(
                f"https://search.yahoo.com/search?p={quote_plus(q)}",
                impersonate="chrome120",
                timeout=12,
            )
            if s_resp.status_code == 200:
                raw_html = unquote(s_resp.text)
                found = re.findall(
                    r"https?://(?:stage\.)?finance\.yahoo\.com/quote/[A-Za-z0-9.\-]+/earnings/[A-Za-z0-9.\-_]+\.html",
                    raw_html,
                    re.I,
                )
                candidate_urls.extend(found)
        except Exception:
            pass

    for full_url in candidate_urls:
        try:
            t_resp = cf_requests.get(full_url, impersonate="chrome120", timeout=15)
            if t_resp.status_code != 200:
                continue
            t_soup = BeautifulSoup(t_resp.text, "lxml")
            article = (
                t_soup.find("article")
                or t_soup.find("div", {"class": re.compile(r"caas-body|body|transcript", re.I)})
                or t_soup
            )
            paragraphs = article.find_all("p")
            segments = []
            lines = []
            for idx, p in enumerate(paragraphs, 1):
                txt = p.get_text(" ", strip=True)
                if len(txt) < 25:
                    continue
                speaker = "Speaker"
                content = txt
                if ":" in txt[:65]:
                    parts = txt.split(":", 1)
                    speaker = parts[0].strip()
                    content = parts[1].strip()
                segments.append({"paragraph_number": idx, "speaker": speaker, "content": content})
                lines.append(f"[{speaker}]: {content}")

            if len(segments) >= 5:
                m = re.search(r"Q([1-4])-(\d{4})", full_url, re.I)
                fq = int(m.group(1)) if m else ((datetime.now().month - 1) // 3 + 1)
                fy = int(m.group(2)) if m else datetime.now().year
                return {
                    "symbol": sym,
                    "fiscal_year": fy,
                    "fiscal_quarter": fq,
                    "report_date": datetime.now().strftime("%Y-%m-%d"),
                    "source": "Yahoo Finance (Live Direct)",
                    "segments": segments,
                    "raw_text": "\n\n".join(lines),
                }
        except Exception as e:
            logger.warning(f"Yahoo transcript parse failed on {full_url}: {e}")

    return None


# ---------------------------------------------------------------------------
# Engine 3: DuckDB Parquet Archive Fallback (with custom_user_agent & HF_TOKEN)
# ---------------------------------------------------------------------------
def configure_duckdb_con():
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET custom_user_agent = '{BROWSER_UA}';")
    con.execute("SET http_keep_alive = false;")
    if HF_TOKEN:
        con.execute(
            f"""
            CREATE SECRET hf_secret (
                TYPE HTTP,
                EXTRA_HTTP_HEADERS MAP {{'Authorization': 'Bearer {HF_TOKEN}'}}
            );
            """
        )
    return con


def fetch_transcript_from_parquet(
    symbol: str, fiscal_year: Optional[int] = None, fiscal_quarter: Optional[int] = None
) -> Optional[dict]:
    try:
        con = configure_duckdb_con()
        if fiscal_year and fiscal_quarter:
            query = f"""
                SELECT symbol, fiscal_year, fiscal_quarter, report_date, to_json(transcripts)
                FROM '{HF_TRANSCRIPTS_PARQUET}'
                WHERE symbol = ? AND fiscal_year = ? AND fiscal_quarter = ?
                LIMIT 1
            """
            row = con.execute(query, [symbol.upper(), fiscal_year, fiscal_quarter]).fetchone()
        else:
            query = f"""
                SELECT symbol, fiscal_year, fiscal_quarter, report_date, to_json(transcripts)
                FROM '{HF_TRANSCRIPTS_PARQUET}'
                WHERE symbol = ?
                ORDER BY report_date DESC
                LIMIT 1
            """
            row = con.execute(query, [symbol.upper()]).fetchone()
        con.close()

        if not row:
            return None

        segments = json.loads(row[4]) if row[4] else []
        lines = [
            f"[{seg.get('speaker', 'Speaker')}]: {seg.get('content', '')}" for seg in segments
        ]
        raw_text = "\n\n".join(lines)
        if not raw_text.strip():
            return None

        return {
            "symbol": row[0],
            "fiscal_year": int(row[1]),
            "fiscal_quarter": int(row[2]),
            "report_date": str(row[3]),
            "source": "Yahoo Finance (Parquet Archive)",
            "segments": segments,
            "raw_text": raw_text,
        }
    except Exception as e:
        logger.warning(f"Parquet archive fallback skipped for {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Local LLM Summarizer (Ollama @ 192.168.86.35:11434)
# ---------------------------------------------------------------------------
async def get_available_ollama_models() -> List[str]:
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        logger.warning(f"Could not reach Ollama at {OLLAMA_BASE_URL}: {e}")
    return []


async def resolve_ollama_model(preferred: Optional[str] = None) -> str:
    target = preferred or DEFAULT_OLLAMA_MODEL
    models = await get_available_ollama_models()
    if not models:
        return target
    if target in models:
        return target
    for m in models:
        if m.startswith(target.split(":")[0]):
            return m
    return models[0]


async def summarize_with_ollama(
    company_name: str,
    symbol: str,
    fiscal_year: int,
    fiscal_quarter: int,
    raw_text: str,
    model_override: Optional[str] = None,
) -> dict:
    model = await resolve_ollama_model(model_override)

    max_chars = 42000
    if len(raw_text) > max_chars:
        half = max_chars // 2
        trimmed_text = (
            raw_text[:half]
            + "\n\n[... Middle section condensed for context window ...]\n\n"
            + raw_text[-half:]
        )
    else:
        trimmed_text = raw_text

    system_prompt = (
        "You are a senior equity research analyst. Analyze the provided earnings call transcript "
        "and respond using the exact XML tags below. Do not omit the tags.\n\n"
        "<SENTIMENT>Bullish, Neutral, or Bearish</SENTIMENT>\n"
        "<SNIPPET>\n"
        "Write a punchy 2-3 sentence executive summary (max 70 words) suitable for a dashboard card. "
        "State key revenue/EPS performance, primary growth catalyst, and forward guidance tone.\n"
        "</SNIPPET>\n"
        "<SUMMARY>\n"
        "### 1. Executive Summary & Headline Performance\n"
        "- Key takeaways, top/bottom-line highlights, and management tone.\n\n"
        "### 2. Forward Guidance & Financial Outlook\n"
        "- Specific revenue, margin, EPS, or CapEx guidance changes mentioned.\n\n"
        "### 3. Segment & Product Highlights\n"
        "- Performance broken down by business unit, geography, or product line.\n\n"
        "### 4. Headwinds, Risks & Margin Pressures\n"
        "- Macro challenges, supply chain, FX, or competitive pressures.\n\n"
        "### 5. Analyst Q&A Takeaways\n"
        "- Key questions where analysts pressed management and how executives responded.\n"
        "</SUMMARY>"
    )

    user_prompt = (
        f"Company: {company_name} ({symbol})\n"
        f"Period: Q{fiscal_quarter} {fiscal_year}\n\n"
        f"TRANSCRIPT:\n{trimmed_text}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": 16384,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")

        clean_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        sentiment_match = re.search(r"<SENTIMENT>(.*?)</SENTIMENT>", clean_content, re.DOTALL | re.I)
        snippet_match = re.search(r"<SNIPPET>(.*?)</SNIPPET>", clean_content, re.DOTALL | re.I)
        summary_match = re.search(r"<SUMMARY>(.*?)</SUMMARY>", clean_content, re.DOTALL | re.I)

        sentiment = sentiment_match.group(1).strip() if sentiment_match else "Neutral"
        if "bull" in sentiment.lower():
            sentiment = "Bullish"
        elif "bear" in sentiment.lower():
            sentiment = "Bearish"
        else:
            sentiment = "Neutral"

        snippet = (
            snippet_match.group(1).strip()
            if snippet_match
            else clean_content[:280].replace("#", "").strip() + "..."
        )
        summary_md = summary_match.group(1).strip() if summary_match else clean_content

        return {
            "sentiment": sentiment,
            "snippet": snippet,
            "summary_markdown": summary_md,
            "model_used": model,
        }
    except Exception as e:
        logger.error(f"Ollama summarization error ({OLLAMA_BASE_URL}): {e}")
        return {
            "sentiment": "Neutral",
            "snippet": f"Transcript retrieved ({len(raw_text):,} chars), but local LLM at {OLLAMA_BASE_URL} was unreachable ({e}). Click Re-Summarize once Ollama is online.",
            "summary_markdown": f"⚠️ **Local LLM Connection Error:** Could not summarize via `{OLLAMA_BASE_URL}` (`{e}`).\n\nFull transcript is available in the **Verbatim Transcript** tab.",
            "model_used": model,
        }


# ---------------------------------------------------------------------------
# Core Pipeline: Fetch + Summarize + Persist
# ---------------------------------------------------------------------------
async def process_company_transcript(
    symbol: str,
    fiscal_year: Optional[int] = None,
    fiscal_quarter: Optional[int] = None,
    force_resummarize: bool = False,
    model_override: Optional[str] = None,
    direct_fool_url: Optional[str] = None,
) -> dict:
    comp = resolve_company(symbol) or {"symbol": symbol.upper(), "name": symbol.upper(), "is_sp500": 0}
    sym = comp["symbol"]

    if not force_resummarize and not (fiscal_year and fiscal_quarter) and not direct_fool_url:
        with get_db() as conn:
            cached = conn.execute(
                "SELECT * FROM transcripts WHERE symbol = ? ORDER BY report_date DESC LIMIT 1",
                (sym,),
            ).fetchone()
            if cached and cached["summary_markdown"] and not cached["summary_markdown"].startswith("⚠️"):
                return dict(cached)

    t_data = None

    # 1. If a direct Motley Fool transcript URL was discovered during feed sync, use it first
    if direct_fool_url:
        t_data = await asyncio.to_thread(parse_fool_transcript_article, direct_fool_url, sym)

    # 2. Tier 1: Motley Fool Live Resolver (Feed + Quote Page + Search)
    if not t_data:
        t_data = await asyncio.to_thread(find_fool_transcript_for_symbol, sym)

    # 3. Tier 2: Yahoo Finance Live Direct Scraper
    if not t_data:
        t_data = await asyncio.to_thread(fetch_transcript_from_yahoo_live, sym)

    # 4. Tier 3: DuckDB Hugging Face Parquet Archive
    if not t_data:
        t_data = await asyncio.to_thread(fetch_transcript_from_parquet, sym, fiscal_year, fiscal_quarter)

    if not t_data:
        raise HTTPException(
            status_code=404,
            detail=f"No earnings call transcript found across Motley Fool or Yahoo Finance for {sym}.",
        )

    fy = t_data["fiscal_year"]
    fq = t_data["fiscal_quarter"]
    record_id = f"{sym}_{fy}_Q{fq}"

    if not force_resummarize:
        with get_db() as conn:
            existing = conn.execute("SELECT * FROM transcripts WHERE id = ?", (record_id,)).fetchone()
            if existing and existing["summary_markdown"] and not existing["summary_markdown"].startswith("⚠️"):
                return dict(existing)

    llm_res = await summarize_with_ollama(
        company_name=comp["name"],
        symbol=sym,
        fiscal_year=fy,
        fiscal_quarter=fq,
        raw_text=t_data["raw_text"],
        model_override=model_override,
    )

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    record = {
        "id": record_id,
        "symbol": sym,
        "company_name": comp["name"],
        "fiscal_year": fy,
        "fiscal_quarter": fq,
        "report_date": t_data["report_date"],
        "source": t_data["source"],
        "is_sp500": comp["is_sp500"],
        "raw_text": t_data["raw_text"],
        "segments_json": json.dumps(t_data["segments"]),
        "sentiment": llm_res["sentiment"],
        "snippet": llm_res["snippet"],
        "summary_markdown": llm_res["summary_markdown"],
        "model_used": llm_res["model_used"],
        "updated_at": now_str,
    }

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO transcripts (
                id, symbol, company_name, fiscal_year, fiscal_quarter, report_date,
                source, is_sp500, raw_text, segments_json, sentiment, snippet,
                summary_markdown, model_used, updated_at
            ) VALUES (
                :id, :symbol, :company_name, :fiscal_year, :fiscal_quarter, :report_date,
                :source, :is_sp500, :raw_text, :segments_json, :sentiment, :snippet,
                :summary_markdown, :model_used, :model_used
            )
            """.replace(":model_used, :model_used", ":model_used, :updated_at"),
            record,
        )
        conn.commit()

    return record


RECENT_SP500_LIMIT = int(os.getenv("RECENT_SP500_LIMIT", "9"))


async def sync_recent_sp500_job():
    """Finds the 9 most recent S&P 500 earnings calls from live transcript feeds and summarizes them."""
    if sync_status["running"]:
        return
    sync_status["running"] = True
    sync_status["message"] = (
        f"Scanning live transcript feeds for the {RECENT_SP500_LIMIT} newest S&P 500 calls..."
    )
    logger.info(sync_status["message"])

    try:
        sp500_set = get_sp500_set()
        # Scan up to 8 pages to ensure we capture at least 9 S&P 500 constituents
        discovered = await asyncio.to_thread(scrape_fool_listing_pages, 8)

        sp500_candidates = []
        seen_syms = set()
        for item in discovered:
            sym = item["symbol"]
            if sym in sp500_set and sym not in seen_syms:
                seen_syms.add(sym)
                sp500_candidates.append(item)
            if len(sp500_candidates) >= RECENT_SP500_LIMIT:
                break

        # Fallback seed tickers if fewer than 9 S&P 500 companies appeared in recent pages
        fallback_list = [
            "GE", "NFLX", "RTX", "COF", "MSCI", "STLD",
            "GPC", "JPM", "TSLA", "NVDA", "AAPL", "MSFT",
        ]
        for fallback_sym in fallback_list:
            if len(sp500_candidates) >= RECENT_SP500_LIMIT:
                break
            if fallback_sym not in seen_syms:
                seen_syms.add(fallback_sym)
                sp500_candidates.append({"symbol": fallback_sym, "url": None})

        total = RECENT_SP500_LIMIT
        for idx, item in enumerate(sp500_candidates[:total], 1):
            sym = item["symbol"]
            sync_status["message"] = (
                f"Processing ({idx}/{total}): {sym} via Ollama ({OLLAMA_BASE_URL})..."
            )
            logger.info(sync_status["message"])
            try:
                await process_company_transcript(
                    sym,
                    force_resummarize=False,
                    direct_fool_url=item.get("url"),
                )
            except Exception as inner_e:
                logger.warning(f"Failed syncing S&P 500 item {sym}: {inner_e}")

        sync_status["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sync_status["message"] = "Up to date"
    except Exception as e:
        logger.error(f"S&P 500 background sync failed: {e}")
        sync_status["message"] = f"Sync error: {e}"
    finally:
        sync_status["running"] = False


# ---------------------------------------------------------------------------
# FastAPI Lifespan & Endpoints
# ---------------------------------------------------------------------------
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await seed_companies()
    scheduler.add_job(sync_recent_sp500_job, "interval", hours=SYNC_INTERVAL_HOURS)
    scheduler.start()
    asyncio.create_task(sync_recent_sp500_job())
    yield
    scheduler.shutdown()


app = FastAPI(title="S&P 500 Earnings Call AI Summarizer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.get("/api/status")
async def api_status():
    models = await get_available_ollama_models()
    active_model = await resolve_ollama_model()
    return {
        "ollama_url": OLLAMA_BASE_URL,
        "ollama_online": len(models) > 0,
        "available_models": models,
        "active_model": active_model,
        "sync_status": sync_status,
    }


@app.get("/api/recent-sp500")
async def get_recent_sp500():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, company_name, fiscal_year, fiscal_quarter,
                   report_date, source, sentiment, snippet, model_used, updated_at
            FROM transcripts
            WHERE is_sp500 = 1
            ORDER BY report_date DESC, updated_at DESC
            LIMIT 9
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/sync-sp500")
async def trigger_sp500_sync(background_tasks: BackgroundTasks):
    if not sync_status["running"]:
        background_tasks.add_task(sync_recent_sp500_job)
    return {"status": "started", "sync_status": sync_status}


@app.get("/api/search-companies")
async def search_companies(q: str = Query(..., min_length=1)):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT symbol, name, is_sp500
            FROM companies
            WHERE UPPER(symbol) LIKE UPPER(?) OR UPPER(name) LIKE UPPER(?)
            ORDER BY
                CASE WHEN UPPER(symbol) = UPPER(?) THEN 0
                     WHEN UPPER(symbol) LIKE UPPER(?) THEN 1
                     ELSE 2 END,
                is_sp500 DESC,
                LENGTH(symbol) ASC
            LIMIT 10
            """,
            (f"{q}%", f"%{q}%", q, f"{q}%"),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/transcript/{query_str}")
async def get_or_fetch_transcript(
    query_str: str,
    force: bool = False,
    model: Optional[str] = None,
):
    comp = resolve_company(query_str)
    if not comp:
        raise HTTPException(status_code=404, detail=f"Company '{query_str}' not found.")
    record = await process_company_transcript(
        comp["symbol"],
        force_resummarize=force,
        model_override=model,
    )
    return record
