# dlmodel

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](backend/pyproject.toml)
[![Release](https://img.shields.io/badge/release-v0.4.3-green)](https://github.com/sreyun/dlmodel/tags)
[![CI](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml/badge.svg)](https://github.com/sreyun/dlmodel/actions/workflows/docker-swr.yml)
[![Image](https://img.shields.io/badge/image-Huawei%20SWR-orange)](#deployment--images)

[简体中文](README.md) · **English** · [日本語](README.ja.md) · [Русский](README.ru.md)

**dlmodel is a self-hosted LLM weight download manager**: in network-restricted environments it reliably pulls models from ModelScope / Hugging Face via multi-source fallback + aria2 multi-connection downloads, stores them in the vLLM layout, and manages queue, resume and pause/recover through a web UI — then hands them straight to vLLM / Ollama inference.

<!-- TODO: insert UI screenshot/GIF (task list, download progress, pause/resume). Suggested path docs/screenshot.png:
     ![dlmodel screenshot](docs/screenshot.png)
-->

## Features

- 🔄 **Automatic multi-source fallback** — the `auto` source probes ModelScope first, then falls back to the HF mirror; within each source it degrades step by step: **aria2 multi-connection → single-stream HTTP resume → native SDK**, connecting to hf-mirror.com / ModelScope directly with no proxy required
- ⏸️ **Pause / resume / interrupted downloads** — `paused` is a first-class state: a graceful stop of the network stream, not a cancel; the task record, progress, downloaded files, resume checkpoints and logs are kept, and resuming continues from the breakpoint (aria2 `continue` / HTTP `Range` / SDK `.temp` / Ollama server-side cache)
- 📊 **Download queue** — concurrent tasks (1–8), multi-connection per file, progress / speed / ETA (the total size is derived from each source's metadata; when it cannot be derived the UI says so instead of showing a fake percentage), retry on failure, exponential-backoff retries for transient TLS/network errors
- 🛡️ **Graceful restart** — on shutdown all `queued` / `running` tasks are parked in SQLite and automatically re-queued on next start; upgrades and restarts never lose tasks
- 🔑 **Bearer token auth** — constant-time comparison + startup gate (a too-short or weak-password token refuses to boot) + rate limiting on failed auth (429)
- 🤖 **Inference integration** — one-command Compose profiles for Ollama / vLLM; the UI generates a vLLM launch command from the downloaded model path (path + port, shell-safe quoting); model library scanning and Ollama local model list / pull
- 📱 **Chat-bot notifications** — DingTalk / Feishu (Lark) / WeCom group robots with per-event toggles, outbound host allowlist validation and masked token echo
- 🗃️ **Zero external dependencies** — tasks and settings persist in SQLite (WAL); `docker compose up -d` is a complete deployment — no database, message queue or Node build chain

**Tech stack**: FastAPI + uvicorn + httpx (async) · aria2 JSON-RPC · aiosqlite · huggingface_hub / modelscope SDK fallback · build-free native JS SPA (no external static assets, fully locked CSP)

## Quick start

Works on Linux / macOS / Windows.

```bash
# 1. Clone
git clone https://github.com/sreyun/dlmodel.git
cd dlmodel

# 2. Configure (key step: set a strong ADMIN_TOKEN)
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # generate a strong token, put it in .env

# 3. Start (uses Huawei SWR accelerated images by default; if your SWR org is private, run
#    docker login swr.cn-east-3.myhuaweicloud.com first)
docker compose up -d

# 4. Open the browser and log in with ADMIN_TOKEN
#    http://<server-ip>:8080
```

Create a download task (source `auto`, target `vLLM`); once it completes, the on-disk path is shown on the task card (also under the Models page) and can be loaded by vLLM / Ollama directly.

> ⚠️ **Security**: the web UI binds `0.0.0.0:8080` by default. Before exposing it publicly, set a long random `ADMIN_TOKEN` in `.env` (tokens shorter than 12 characters or on the weak-password list make the service **refuse to start**), and put it behind a reverse proxy / HTTPS. Change `ARIA2_RPC_SECRET` to a unique value as well.

## Architecture

```
Browser SPA (frontend/, build-free native JS, short polling)
   │  Bearer token
   ▼
FastAPI routes ── /api/downloads* /api/settings /api/models /api/services/* /api/health
   │
   ├─ DownloadQueue: asyncio worker pool (concurrency 1–8), state machine
   │     queued → running → completed | failed | cancelled
   │        ▲ pause (stop stream gracefully, keep checkpoints/progress)   │
   │        └────────────── paused ◄──────────────────────────────────────┘──► resume → queued
   │
   ├─ Downloaders: source auto → ModelScope ⇁ HF mirror; per source: aria2 → HTTP Range → SDK fallback
   ├─ Control plane: cooperative cancel / pause (signals checked in on_progress / on_log),
   │                  never swallowed by retries or fallback
   ├─ aria2 container (JSON-RPC, 16 connections/file, .aria2 control-file resume)
   └─ SQLite (WAL): tasks + settings; queued/running parked on shutdown, re-queued on boot
Volumes: ./data/models (weights, shared with vLLM/Ollama) · ./data/app (DB) · ./data/aria2
```

State consistency converges via CAS updates (`UPDATE … WHERE status IN (…)`): any interleaving of pause / resume / cancel between queued and running lets exactly one side win; workers re-check ownership before reporting, so cancelled/reclaimed tasks can't overwrite state. Terminal and paused states release all in-memory bookkeeping (cancel/pause flags, aria2 gid maps, notification de-dup sets) — no leaks over long uptime.

## Configuration reference

All configuration is provided via `.env` in the repository root (defaults in parentheses):

| Variable | Default | Description |
|----------|---------|-------------|
| `ADMIN_TOKEN` | `changeme` | Bearer token for UI / API — **must be changed**; <12 chars or weak → refuses to start |
| `MODEL_ROOT` | `/models` | Model weights root (compose: `./data/models`) |
| `DATA_DIR` | `/data` | SQLite `app.db` and settings (compose: `./data/app`) |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HF mirror endpoint |
| `HF_TOKEN` | empty | For gated models (mirror endpoint must support token pass-through) |
| `MODELSCOPE_API_TOKEN` | empty | ModelScope API token |
| `ARIA2_RPC_URL` | `http://aria2:6800/jsonrpc` | aria2 RPC endpoint |
| `ARIA2_RPC_SECRET` | `dlmodel` | Must match the aria2 service; change in production |
| `DOWNLOAD_CONCURRENCY` | `2` | Concurrent download tasks (hot-adjustable 1–8 via API) |
| `ARIA2_CONNECTIONS` | `16` | Connections per file |
| `DOWNLOAD_RETRIES` | `3` | Extra retries for transient TLS/network errors (exponential backoff; 0 = off) |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama endpoint (profile or remote) |
| `VLLM_BASE_URL` | `http://vllm:8000` | vLLM endpoint (profile or remote) |
| `VLLM_MODEL` | empty | The **concrete downloaded repo path** used by `--profile vllm` |
| `NOTIFY_DINGTALK_WEBHOOK` / `NOTIFY_FEISHU_WEBHOOK` / `NOTIFY_WECOM_WEBHOOK` | empty | Group-robot webhooks (UI settings take precedence) |
| `NOTIFY_ON_COMPLETED` / `NOTIFY_ON_FAILED` | `1` | Push toggles: completed / failed |
| `NOTIFY_ON_STARTED` / `NOTIFY_ON_CANCELLED` | `0` | Push toggles: started / cancelled |
| `DLMODEL_IMAGE` | SWR `:latest` | Production image; pin `:vX.Y.Z` for rollback |
| `ARIA2_IMAGE` | `p3terx/aria2-pro` via SWR | aria2 image (SWR-accelerated by default since v0.4.1, proxy-free pull) |
| `WEB_BIND` / `WEB_PORT` | `0.0.0.0` / `8080` | Production web bind and port |
| `PUID` / `PGID` | `0` / `0` | aria2 run identity; must match write permissions on the model volume |

`HF_TOKEN` / `MODELSCOPE_API_TOKEN` saved in the Settings page (stored in SQLite) take precedence over the same-name environment variables, so they can be changed at runtime.

<details>
<summary>Data & directory layout</summary>

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `./data/models` | `/models` (`MODEL_ROOT`) | Model weights, shared with vLLM / Ollama |
| `./data/app` | `/data` (`DATA_DIR`) | SQLite `app.db`, settings |
| `./data/aria2` | `/config` | aria2 config, session and tracker log |

Disk layout: vLLM / HF models go to `{MODEL_ROOT}/hf/<org>/<repo>/`; Ollama models go to `{MODEL_ROOT}/ollama` (mounted as `/root/.ollama` in the Ollama container). Delete task records in the UI (cleaning terminal states removes records only, never downloaded files), or clean the directories above yourself.

</details>

## API overview

Except `GET /api/health`, all endpoints require `Authorization: Bearer <ADMIN_TOKEN>`.

| Method & path | Description |
|---------------|-------------|
| `POST /api/downloads` | Create a task. Body `{name, source: auto\|modelscope\|huggingface\|ollama, target: vllm\|ollama, revision?}`; `400` if an active task (incl. `paused`) with the same name+target exists |
| `GET /api/downloads` | Task list (incl. `progress_bytes` / `total_bytes` / `speed_bps`) |
| `GET /api/downloads/{id}` | Single task |
| `POST /api/downloads/{id}/pause` | Pause a `queued` / `running` task (`400` = invalid state) |
| `POST /api/downloads/{id}/resume` | Resume a `paused` task back into the queue |
| `POST /api/downloads/{id}/cancel` | Cancel (works from active states and `paused`) |
| `POST /api/downloads/{id}/retry` | Retry a `failed` / `cancelled` task; re-queued and resumed from checkpoint |
| `DELETE /api/downloads/{id}` | Delete the task record (terminal states and `paused`; records only, files untouched) |
| `POST /api/downloads/cleanup?status=…` | Bulk-clean terminal records (default `completed,cancelled,failed`) |
| `GET /api/downloads/{id}/events` | SSE progress stream (Bearer header only, see [FAQ](#faq--troubleshooting) #4) |
| `GET` / `PUT /api/settings` | Read / write settings (tokens & webhooks masked on read; PUT only applies non-`None` fields) |
| `POST /api/settings/notify-test` | Send a test notification |
| `GET /api/models` · `DELETE /api/models/{id}` | Library scan (path / size) and delete (**removes the model directory on disk**; deleting task records never touches files) |
| `GET /api/services/ollama/health` · `…/models` · `POST …/pull` | Ollama health, local model list, pull (tag override) |
| `GET /api/services/vllm/health` · `…/launch-command` | vLLM health, launch-command generation |
| `POST /api/auth/verify` | Verify the token |
| `GET /api/health` | Liveness probe (auth-free, used by the container healthcheck) |

Concurrency hot-resize: `PUT /api/settings` with `download_concurrency` immediately resizes the queue (1–8).

## Deployment & images

- **Production**: `docker compose up -d` — default image `swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest` (aria2 image is SWR-accelerated as well); `pull_policy: always` refreshes on every `up`; pin `DLMODEL_IMAGE=…:vX.Y.Z` in `.env` for fixed versions or rollback
- **Release CI**: pushing a `v*` tag triggers the GitHub Actions workflow "Build and push Huawei SWR", updating both `:<tag>` and `:latest`; requires Secrets `HW_ACCESS_KEY` / `HW_SECRET_KEY` (optional Variable `HW_SWR_NAMESPACE`, default `sreyun`), and the SWR organization ([console](https://console.huaweicloud.com/swr), region cn-east-3) must exist beforehand; manual rerun: Actions → Run workflow

### Optional inference services

Management and downloads do not depend on inference; enable it via Compose profiles when needed (off by default):

```bash
docker compose --profile ollama up -d      # local API: http://127.0.0.1:11434
docker compose --profile vllm up -d        # OpenAI-compatible: http://127.0.0.1:8000, needs NVIDIA Container Toolkit
```

- `VLLM_MODEL` must be a **concrete downloaded repo path** (e.g. `/models/hf/Qwen/Qwen3-0.6B`), not the `/models/hf` root; set it in `.env` after the download completes and recreate `vllm`
- vLLM defaults to image `vllm/vllm-openai:latest` + `runtime: nvidia`; `HUGGING_FACE_HUB_TOKEN` is passed from `HF_TOKEN`
- Models pulled via Ollama are stored under `{MODEL_ROOT}/ollama` (mounted as `/root/.ollama`), shared with the Ollama download source

## FAQ / Troubleshooting

**1. aria2 unavailable / only a single stream?**
On RPC failure the downloader automatically degrades to single-stream HTTP resume (the task message shows "aria2 unavailable") — the task still downloads. Multi-connection unavailability usually has two causes only: **network issues** or **insufficient permissions**. The default `PUID=0` / `PGID=0` exists to sidestep shared-volume write permission problems; if host directories are too strict, aria2 cannot write `.aria2` control files and the model volume — make sure the container user can write `./data/models` and `./data/aria2`. After fixing, re-run the download: if the "aria2 unavailable" message disappears it has recovered; check `docker compose ps` / `docker compose logs aria2` for the aria2 container state.

**2. HF model 401 / 403 (gated)?**
Set `HF_TOKEN` (accept the model terms on HF, then generate a token) in the Settings page or `.env`, and confirm your mirror endpoint supports token pass-through (hf-mirror.com does); for ModelScope-exclusive models prefer the `auto` or explicit `modelscope` source.

**3. "Another download for this target is still running" after clicking Resume?**
A known ModelScope limitation: its `snapshot_download` runs in a blocking thread that cannot be interrupted immediately, so pausing means "stop tracking and park as `paused`", and the target directory is released only after the background thread finishes. **Resuming the same target directory immediately is rejected (409) to prevent concurrent double-write corruption** — wait a moment and click Resume again. All other sources (HF / aria2 / HTTP / Ollama) can resume instantly from the breakpoint.

**4. There is an SSE endpoint — why does the UI poll?**
`GET /api/downloads/{id}/events` only accepts the Bearer token as a request header, but the browser's native `EventSource` cannot set custom headers, so the UI uses short polling (1 s active / 4 s idle). For real-time push, integrate `fetch` + ReadableStream yourself.

**5. Port already in use?**
Set `WEB_PORT` in `.env` (and `WEB_BIND` for the bind address), then re-run `docker compose up -d`.

**6. Not enough disk space (No space left on device)?**
The cause surfaces in the task message and push notifications. Note: **disk-full and permission-denied errors are never retried** (retrying would be pointless); after freeing space, click Retry on the failed task to continue from the checkpoint. Check `df -h` before big downloads (models are easily tens of GB).

**7. Where did my running tasks go after a container restart?**
On start the queue uniformly re-queues every `queued` / `running` task and re-schedules it (message "Re-queued after service restart…", covering both graceful shutdown and hard stops like `docker compose down`); aria2 recovers in-flight transfers, HTTP / SDK resume from on-disk checkpoints. `paused` tasks stay paused across restarts and wait for you to click Resume.

**8. A task keeps failing — how do I find out why?**
Each task's `message` shows the failure cause or current stage (source resolution, fallback, retry #N), visible via `GET /api/downloads/{id}`; server logs (`docker compose logs web`, JSON) carry the full timeline with task IDs, tokens masked out.

## Roadmap

Under consideration (not commitments): SSE front-end integration (fetch-stream approach), bandwidth throttling, bulk manifest import, scheduled cleanup policies, task tags & grouping.

## Contributing

Issues and PRs are welcome: please include test coverage, and keep this document and all language versions (`README.md` / `README.ja.md` / `README.ru.md`) in sync with behavior changes; commit messages follow Conventional Commits (`feat:` / `fix:` / `chore:`…).

## License

[MIT License](LICENSE) © 2026 sreyun
