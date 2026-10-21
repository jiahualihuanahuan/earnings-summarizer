# Earnings Call AI Terminal

Local-first S&P 500 earnings call transcript fetcher and AI summarizer.

Pulls recent earnings call transcripts, stores them, and generates concise key-point summaries using a local Ollama model (default: Qwen3.5 9B). Built for Canadian/US passive investors who want structured insights without sending transcripts to the cloud.

## Features

- **Auto-discovery** of recent S&P 500 earnings calls from The Motley Fool transcript listings
- **Verbatim transcript parsing** into speaker turns
- **Local LLM summarization** via Ollama (sentiment + snippet + markdown key points)
- **Background sync** every N hours for the newest S&P 500 calls
- **Search any company** by ticker or name
- **Clean dark-mode web UI** (Tailwind) with summary / full transcript tabs
- **SQLite** persistence (companies + transcripts)
- **Docker Compose** ready

## Prerequisites

- Docker & Docker Compose (recommended), **or** Python 3.11+
- A running [Ollama](https://ollama.com) instance with a model pulled (e.g. `qwen3.5:9b`, `qwen2.5:7b`, `llama3.1:8b`)
- Network access to Motley Fool and (optionally) Hugging Face for ticker catalogs

## Quick Start (Docker)

1. Clone the repo:
   ```bash
   git clone https://github.com/jiahualihuanahuan/earnings-summarizer.git
   cd earnings-summarizer
   ```

2. Edit `docker-compose.yml` and set `OLLAMA_BASE_URL` to your Ollama host (default assumes `http://192.168.86.35:11434`).

3. Start:
   ```bash
   docker compose up -d --build
   ```

4. Open http://localhost:8085

The container exposes port 8000 internally, mapped to 8085 on the host. Data is persisted in `./data/earnings.db`.

## Manual Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set environment variables (optional)
export OLLAMA_BASE_URL=http://localhost:11434
export OLLAMA_MODEL=qwen3.5:9b
export DATABASE_PATH=./data/earnings.db

# Run
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://192.168.86.35:11434` | Base URL of your Ollama server |
| `OLLAMA_MODEL` | `qwen3.5:9b` | Model name to use for summarization |
| `SYNC_INTERVAL_HOURS` | `6` | How often the background S&P 500 sync runs |
| `DATABASE_PATH` | `/app/data/earnings.db` | SQLite database path |
| `HF_TOKEN` | (empty) | Optional Hugging Face token (for higher rate limits) |

## API Endpoints

- `GET /` — Web UI
- `GET /api/status` — Ollama connectivity + current sync status
- `GET /api/recent-sp500` — Latest summarized S&P 500 earnings calls
- `POST /api/sync-sp500` — Trigger a background sync immediately
- `GET /api/search-companies?q=` — Company autocomplete
- `GET /api/transcript/{symbol}?force=false&model=` — Fetch + summarize a company (or return cached)

## Architecture

- **FastAPI** backend (`app/main.py`)
- **Motley Fool scraper** (curl_cffi + BeautifulSoup) for live transcripts
- **Ollama** for local summarization (structured output with `<SENTIMENT>`, `<SNIPPET>`, `<SUMMARY>`)
- **SQLite** for companies and transcripts
- **APScheduler** for periodic S&P 500 sync
- **Static frontend** (`app/static/index.html`) with Tailwind + marked.js
- Optional Hugging Face parquet/JSON sources for broader ticker coverage

## Notes

- Transcript scraping depends on Motley Fool page structure; it may need updates if the site changes.
- Summarization quality depends on the local model and transcript length. Larger models generally produce better key-point summaries.
- The background job currently targets the most recent S&P 500 calls discovered in the listing pages, with a small fallback ticker list.
- `data/earnings.db` is included for convenience; delete it to start fresh.

## License

Personal / research use. Not affiliated with The Motley Fool or any data provider.
