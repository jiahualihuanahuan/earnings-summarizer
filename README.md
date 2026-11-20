# Earnings Call AI Terminal

Local-first S&P 500 earnings call transcript fetcher and AI summarizer.

Pulls recent earnings call transcripts from multiple sources (Seeking Alpha, Benzinga, Motley Fool, Yahoo Finance) and generates concise key-point summaries using a local LLM. The default backend is [Ollama](https://ollama.com) on the homelab at `http://192.168.86.35:11434` with model `qwen3.5:9b` (no API key). A vLLM OpenAI-compatible server remains a commented alternative. Built for Canadian/US passive investors who want structured insights without sending transcripts to the cloud.

## Features

- **Multi-source discovery**: Prioritizes Seeking Alpha and Benzinga for faster mega-cap transcripts, falls back to Motley Fool and Yahoo Finance
- **Verbatim transcript parsing** into speaker turns (where available)
- **Local LLM summarization** via Ollama's native `/api/chat` API (sentiment + snippet + markdown key points). Qwen thinking mode is disabled by default for speed (`think: false`); residual `<think>` tags are still stripped.
- **Hourly background sync** for the newest S&P 500 calls
- **Search any company** by ticker or name
- **Clean dark-mode web UI** (Tailwind) with summary / full transcript tabs
- **SQLite** persistence (companies + transcripts)
- **Docker Compose** ready

## Prerequisites

- Docker & Docker Compose (recommended), **or** Python 3.11+
- A running [Ollama](https://ollama.com) server with `qwen3.5:9b` pulled, reachable at `http://192.168.86.35:11434`
  - Ollama must listen on `0.0.0.0` (not just localhost) so Docker containers on the LAN can reach it
  - Start example:
    ```bash
    OLLAMA_HOST=0.0.0.0 OLLAMA_CONTEXT_LENGTH=8192 ollama serve
    ollama pull qwen3.5:9b
    ```
  - From inside Docker, use the host LAN IP `192.168.86.35`, not `localhost`
- Network access to transcript sites and (optionally) Hugging Face for ticker catalogs

### Optional: vLLM instead of Ollama

To switch back to [vLLM](https://docs.vllm.ai) serving `cyankiwi/Qwen3.5-9B-AWQ-4bit` as `qwen3.5-9b` at `http://192.168.86.35:8001/v1`:

1. Set `USE_OLLAMA=false`
2. Set `LLM_BASE_URL=http://192.168.86.35:8001/v1` and `LLM_MODEL=qwen3.5-9b`
3. Set `LLM_API_KEY` only if your vLLM server requires one (do not commit secrets)
4. Example start: `vllm serve cyankiwi/Qwen3.5-9B-AWQ-4bit --served-model-name qwen3.5-9b --port 8001`

These settings are also commented in `docker-compose.yml` and `.env.example`.

## Quick Start (Docker)

1. Clone the repo:
   ```bash
   git clone https://github.com/jiahualihuanahuan/earnings-summarizer.git
   cd earnings-summarizer
   ```

2. Edit `docker-compose.yml` if needed. Defaults use hourly sync and Ollama at `http://192.168.86.35:11434` (`qwen3.5:9b`).

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

# Optional: copy .env.example and export the values, or set them here:
export USE_OLLAMA=true
export OLLAMA_BASE_URL=http://192.168.86.35:11434
export OLLAMA_MODEL=qwen3.5:9b
export OLLAMA_THINK=false
export SYNC_INTERVAL_HOURS=1
export DATABASE_PATH=./data/earnings.db

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_OLLAMA` | `true` | Use Ollama (`true`) or the vLLM OpenAI-compatible backend (`false`) |
| `OLLAMA_BASE_URL` | `http://192.168.86.35:11434` | Ollama server URL (native `/api/chat` and `/api/tags`; no `/v1` suffix) |
| `OLLAMA_MODEL` | `qwen3.5:9b` | Ollama model name for summarization |
| `OLLAMA_THINK` | `false` | When `false`, the Ollama request sends `think: false` to skip Qwen thinking for faster summaries |
| `LLM_BASE_URL` | `http://192.168.86.35:8001/v1` | Base URL of the OpenAI-compatible vLLM server (used only when `USE_OLLAMA=false`) |
| `LLM_API_KEY` | (empty) | Optional API key for vLLM. Leave empty unless the server requires one |
| `LLM_MODEL` | `qwen3.5-9b` | vLLM served model name (used only when `USE_OLLAMA=false`) |
| `SYNC_INTERVAL_HOURS` | `1` | How often the background S&P 500 sync runs (hours) |
| `DATABASE_PATH` | `/app/data/earnings.db` | SQLite database path |
| `HF_TOKEN` | (empty) | Optional Hugging Face token (for higher rate limits) |
| `RECENT_SP500_LIMIT` | `9` | Number of recent S&P 500 calls to keep in the dashboard |

## API Endpoints

- `GET /` — Web UI
- `GET /api/status` — LLM connectivity + current sync status
- `GET /api/recent-sp500` — Latest summarized S&P 500 earnings calls
- `POST /api/sync-sp500` — Trigger a background sync immediately
- `GET /api/search-companies?q=` — Company autocomplete
- `GET /api/transcript/{symbol}?force=false&model=` — Fetch + summarize a company (or return cached)

## Architecture

- **FastAPI** backend (`app/main.py`)
- **Multi-source scraper** (curl_cffi + BeautifulSoup): Seeking Alpha → Benzinga → Motley Fool → Yahoo Finance
- **Ollama** (`/api/chat`) for local summarization, with optional **vLLM / OpenAI-compatible** fallback
- **SQLite** for companies and transcripts
- **APScheduler** for periodic (default hourly) S&P 500 sync
- **Static frontend** (`app/static/index.html`) with Tailwind + marked.js

## Notes

- Transcript availability depends on source publication speed. Seeking Alpha and Benzinga are typically fastest for large-cap names.
- Summarization quality depends on the local model and transcript length.
- The Ollama client requests `num_ctx: 8192`, which should match `OLLAMA_CONTEXT_LENGTH=8192` on the server.
- `data/earnings.db` is included for convenience; delete it to start fresh.

## License

Personal / research use. Not affiliated with any data provider.
