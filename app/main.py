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
