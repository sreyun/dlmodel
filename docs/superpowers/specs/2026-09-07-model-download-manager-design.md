# Model Download Manager — Design Spec

**Date:** 2026-09-07  
**Status:** Approved for implementation planning  
**Repo:** `dlmodel`

## 1. Goal

Provide a self-hosted web console to download LLM weights by **model name only**, store them under configurable host directories, accelerate downloads for users in China, and optionally attach/start **vLLM** or **Ollama** inference. Ship with **docker-compose**; management plane and inference plane stay separable.

## 2. Confirmed decisions

| Topic | Choice |
|-------|--------|
| Scope | Download + library management + optional serve/start |
| Sources | ModelScope + Hugging Face (UI selectable); Ollama via `ollama pull` / API |
| Deploy | Management independent; vLLM/Ollama optional attach (compose profiles or remote URLs) |
| Acceleration | Deep: queue, parallelism, aria2 multi-connection, mirror preference, MS→HF fallback |
| Auth | Simple shared `ADMIN_TOKEN` (Bearer), no multi-user accounts |
| Architecture | Monolith FastAPI + light static SPA; aria2 sidecar; optional ollama/vllm profiles |

## 3. Non-goals

- Multi-tenant accounts / RBAC / billing
- Cluster schedulers, multi-GPU autoscaling platforms
- Replacing full Ollama/vLLM UIs (Open WebUI etc.)
- Guaranteeing every HF name maps 1:1 on ModelScope without fallback

## 4. Architecture

Four bounded units:

1. **Web management service** — FastAPI APIs, static UI, Bearer token auth, settings.
2. **Download engine** — in-process task queue + source adapters (HF, ModelScope, Ollama); large files via **aria2**.
3. **Storage volumes** — host-mounted roots, e.g. `{MODEL_ROOT}/hf/...` and `{MODEL_ROOT}/ollama/...`.
4. **Optional inference** — compose profiles or externally configured `OLLAMA_BASE_URL` / `VLLM_BASE_URL`.

```text
Browser ──Bearer──► Web (FastAPI + SPA)
                      │
                      ├─ Task Queue ──► adapters (MS / HF+mirror / Ollama)
                      │                    │
                      │                    └─► aria2 (RPC)
                      │
                      └─ Settings / health ──► Ollama API / vLLM base URL
                      
Host volumes: /models/hf , /models/ollama
Compose profiles: default=web(+aria2); --profile ollama; --profile vllm
```

### 4.1 Components and responsibilities

| Unit | Does | Depends on |
|------|------|------------|
| `web` | Auth, CRUD tasks, list models, settings, proxy progress SSE | SQLite (or JSON/SQLite file), volumes, aria2 RPC, optional inference URLs |
| `aria2` | Multi-connection download, resume, retries | Shared download temp + model volumes |
| `ollama` (optional) | Serve GGUF/Ollama library models | ollama volume |
| `vllm` (optional) | Serve HF-layout checkpoints | hf volume, GPU |

## 5. Download & China acceleration

### 5.1 User input

- **Model name** (required): e.g. `Qwen/Qwen2.5-7B-Instruct` or `llama3.2`
- **Source**: `auto` | `modelscope` | `huggingface` | `ollama`
- **Target runtime**: `vllm` | `ollama`
- Optional: revision/tag, destination override

### 5.2 Source resolution (`auto`)

1. Try ModelScope (exact id or configured name map).
2. On miss/failure after timeout → Hugging Face via domestic endpoint (default `https://hf-mirror.com`).
3. If target is `ollama` and name looks like an Ollama library tag → use Ollama pull path first.

Forced source skips fallback except hard transport errors where retry on same source still applies.

### 5.3 Acceleration features

- `HF_ENDPOINT` defaulting to hf-mirror; overridable in settings/`.env`
- ModelScope SDK + optional `MODELSCOPE_API_TOKEN`
- Hugging Face `HF_TOKEN` for gated models
- **aria2**: multi-connection, continue, retry; configurable connections per file and global task concurrency
- Task queue with progress (bytes, %, speed, ETA), cancel, retry
- Post-download integrity when etag/sha available from source metadata
- Defaults: global concurrent tasks `2`, aria2 connections/file `16`, retries `3`

### 5.4 Layout on disk

- **vLLM / HF layout:** `{MODEL_ROOT}/hf/<org>/<repo>/` (usable as vLLM `--model`)
- **Ollama:** Ollama models dir / API pull into attached instance (`{MODEL_ROOT}/ollama` when using compose profile)

## 6. Inference attach & start

### 6.1 Attach

- Settings: `OLLAMA_BASE_URL`, `VLLM_BASE_URL`, optional keys
- UI health: reachable / unreachable
- Compose profiles share volumes with `web` when used locally; remote URLs allowed

### 6.2 After download

| Target | Behavior |
|--------|----------|
| Ollama | Call Ollama API to pull if needed; list installed models; optional readiness probe |
| vLLM | Do not embed GPU serve inside `web`. Offer copy-paste launch command and/or restart guidance for compose `vllm` service pointing at downloaded HF path. If a reachable management-friendly API exists on configured base URL, use it; otherwise command + docs only |

### 6.3 Compose profiles

- Default: `web` + `aria2`
- `--profile ollama`: Ollama + shared ollama volume
- `--profile vllm`: vLLM (NVIDIA toolkit required) + shared hf volume

## 7. Web UI & auth

### 7.1 Auth

- Env `ADMIN_TOKEN` required at runtime
- Client sends `Authorization: Bearer <token>`; UI prompts once and stores in `localStorage`
- All mutating and listing APIs protected

### 7.2 Pages

1. **Download** — name, source, target, path preview, submit
2. **Tasks** — queue, progress, logs, cancel/retry
3. **Library** — installed models, size, path, runtime tag, delete
4. **Services** — Ollama/vLLM status, installed list, pull/start helpers, copy commands
5. **Settings** — mirrors, concurrency, aria2, HF/MS tokens, model root, inference URLs

### 7.3 Frontend tech

- Static SPA served by FastAPI (vanilla JS or one light framework)
- Progress via **SSE** (preferred) or short polling fallback

## 8. API sketch (illustrative)

- `POST /api/auth/verify` — validate token
- `GET/PUT /api/settings`
- `POST /api/downloads` — `{ name, source, target, revision? }`
- `GET /api/downloads` / `GET /api/downloads/{id}` / `POST .../cancel` / `POST .../retry`
- `GET /api/downloads/{id}/events` — SSE progress
- `GET /api/models` / `DELETE /api/models/{id}`
- `GET /api/services/ollama/health` / `GET .../models` / `POST .../pull`
- `GET /api/services/vllm/health` / `GET .../launch-command?model=...`

Persist tasks and settings in SQLite under a data volume.

## 9. Configuration (`.env`)

| Variable | Purpose |
|----------|---------|
| `ADMIN_TOKEN` | Web auth |
| `MODEL_ROOT` | Host path mounted for models |
| `HF_ENDPOINT` | Default `https://hf-mirror.com` |
| `HF_TOKEN` | Optional |
| `MODELSCOPE_API_TOKEN` | Optional |
| `ARIA2_RPC_URL` | aria2 JSON-RPC |
| `DOWNLOAD_CONCURRENCY` | Default 2 |
| `ARIA2_CONNECTIONS` | Default 16 |
| `OLLAMA_BASE_URL` | Optional attach |
| `VLLM_BASE_URL` | Optional attach |
| `VLLM_MODEL` | Optional default model path for vllm profile |

## 10. Error handling

- Invalid token → 401
- Unknown model on all attempted sources → task `failed` with clear message
- Disk full / permission → fail fast with path hint
- Inference unreachable → UI shows disconnected; download still works
- aria2 down → fail task or fall back to single-stream SDK download (document chosen behavior in implementation: prefer fail with “aria2 unavailable” in v1 for predictability, or SDK fallback if trivial)

**v1 decision:** If aria2 is unavailable, fall back to SDK single-stream download and mark the task with a warning in logs (availability over hard dependency).

## 11. Testing strategy

- Unit: source resolution (`auto` MS→HF), path layout helpers, auth middleware
- Integration: mock HF/MS HTTP; enqueue → progress events → completed path exists
- Manual/compose smoke: `compose up`, submit a tiny public model, see task complete; optional profile health checks

## 12. Repository layout (planned)

```text
dlmodel/
  docker-compose.yml
  .env.example
  README.md
  backend/          # FastAPI app, queue, adapters
  frontend/         # static SPA
  docs/superpowers/specs/...
```

## 13. Success criteria

- User enters a model name, picks source/target, gets a progress UI, files land under configured roots
- China-oriented defaults (MS preference + HF mirror + aria2) work out of the box via compose
- Token gate works without user accounts
- Ollama profile or remote URL can pull/list; vLLM gets correct HF path + launch command / profile hook
- Management works without starting vLLM/Ollama
