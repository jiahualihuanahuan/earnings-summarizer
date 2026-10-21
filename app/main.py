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

# ... [content truncated for this call example - actual will be full]