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
