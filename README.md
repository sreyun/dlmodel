# dlmodel

Download Hugging Face and ModelScope models into host volumes, with a small web UI and an aria2 sidecar. Optional Compose profiles attach Ollama or vLLM to the same model roots.

## Quick start (development)

1. Copy the example env file and set a real admin token:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and change `ADMIN_TOKEN` from `change-me-to-a-long-random-string` to a long random secret.

2. Build and start the **dev** stack (`web` + `aria2`, image built locally):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
   ```

3. Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and sign in with `ADMIN_TOKEN`.

## Production (Huawei SWR image)

CI builds and pushes `swr.cn-east-3.myhuaweicloud.com/<namespace>/dlmodel` on every `v*` tag.

```bash
cp .env.example .env   # set a strong ADMIN_TOKEN; do NOT enable ALLOW_INSECURE_ADMIN
export DLMODEL_IMAGE=swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest
docker login swr.cn-east-3.myhuaweicloud.com
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

CI also pushes version tags (`v0.3.2`, …). Pin those instead of `latest` when you need a fixed rollback target.

Optional Compose convenience:

```bash
# .env
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
DLMODEL_IMAGE=swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest
```

### CI secrets / variables

Repository **Actions secrets** (already used by workflow):

| Secret | Purpose |
|--------|---------|
| `HW_ACCESS_KEY` | Huawei IAM Access Key Id (AK) |
| `HW_SECRET_KEY` | Huawei IAM Secret Access Key (SK) |

Optional:

| Name | Where | Purpose |
|------|-------|---------|
| `HW_SWR_NAMESPACE` | Actions **variable** (preferred) or secret | SWR organization name (default `sreyun`) |

Create the organization once in [SWR console](https://console.huaweicloud.com/swr) (region **华东-上海一 / cn-east-3**) so the first push can create the `dlmodel` repository.

Tag release flow:

```bash
git tag -a v0.3.2 -m "v0.3.2"
git push origin v0.3.2
# → GitHub Action "Build and push Huawei SWR" runs automatically
# → pushes both :v0.3.2 and :latest
```

Manual rebuild: Actions → **Build and push Huawei SWR** → Run workflow.

## China defaults

The example env is oriented toward mainland China networks:

- `HF_ENDPOINT=https://hf-mirror.com` — Hugging Face Hub traffic goes through the mirror.
- Source `auto` tries ModelScope first, then Hugging Face.
- Downloads use aria2 (`ARIA2_RPC_URL=http://aria2:6800/jsonrpc`) with HTTP Range resume + SSL retries if RPC is down or fails.
- Transient TLS errors (common on mirrors) are retried via `DOWNLOAD_RETRIES` (default `3`).

Set `HF_TOKEN` and/or `MODELSCOPE_API_TOKEN` in `.env` for gated or higher-rate pulls. Tokens can also be saved later in the Settings page (SQLite under `DATA_DIR`; UI values override env for merge keys).

## Persistence

Tasks and settings live in SQLite at `{DATA_DIR}/app.db` (compose: `./data/app`). Model files live under `{MODEL_ROOT}` (compose: `./data/models`). On graceful restart, in-flight downloads are **parked** and resumed on next start — they are not cancelled. Delete task records only from the UI (or by removing those data dirs).

## Robot notifications

Settings → 消息推送 supports DingTalk / Feishu / WeCom group robots (HTTPS webhook URLs only). Default events: download completed / failed. Optional: started / cancelled. Started messages include progress, rate, and ETA after first measurable transfer. You can also set `NOTIFY_*` in `.env`; prefer saving in the UI.

## Model roots

Bind mounts (created on first start):

| Host path | Container | Use |
|-----------|-----------|-----|
| `./data/models` | `/models` (`MODEL_ROOT`) | All downloaded weights |
| `./data/app` | `/data` (`DATA_DIR`) | SQLite and app state |
| `./data/aria2` | `/config` | aria2 config |

`web` and `aria2` share `./data/models`. The default stack runs aria2 as root (`PUID=0` / `PGID=0`) so it can write the same files as `web`. If you bind-mount a host directory with restrictive ownership, make `./data/models` writable by the container user (root in the default compose) or downloads fall back to single-stream HTTP.

On-disk layout:

- vLLM / Hugging Face: `{MODEL_ROOT}/hf/<org>/<repo>/`
- Ollama (compose profile): host `./data/models/ollama` → container `/root/.ollama`

## Optional inference profiles

Ollama and vLLM are **not** started by default. Management and downloads work without them.

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile ollama up -d --build
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile vllm up -d --build
```

- **Ollama** (`--profile ollama`) — API at `http://127.0.0.1:11434`. The web service uses `OLLAMA_BASE_URL=http://ollama:11434`.
- **vLLM** (`--profile vllm`) — OpenAI-compatible API at `http://127.0.0.1:8000`.

vLLM notes:

- The compose service uses `runtime: nvidia` and `vllm/vllm-openai:latest`. Adjust the image, runtime, or device requests for your GPU host (NVIDIA Container Toolkit, or a CPU/other image if you are not on NVIDIA).
- `VLLM_MODEL` must be a **concrete downloaded repo path** (for example `/models/hf/org/repo`), not the `/models/hf` root. Set it in `.env` after a download finishes, then recreate the `vllm` service.
- `HUGGING_FACE_HUB_TOKEN` is passed from `HF_TOKEN` if the engine still needs Hub access.

## Configuration

See `.env.example`. Important variables:

| Variable | Purpose |
|----------|---------|
| `ADMIN_TOKEN` | Bearer token for the UI and `/api` |
| `MODEL_ROOT` / `DATA_DIR` | Model and SQLite volumes |
| `DLMODEL_IMAGE` | Production web image (`:latest` recommended; pin `:vX.Y.Z` for rollback) |
| `HF_ENDPOINT` / `HF_TOKEN` | Hugging Face mirror and token |
| `MODELSCOPE_API_TOKEN` | ModelScope token |
| `ARIA2_RPC_URL` / `ARIA2_RPC_SECRET` | aria2 JSON-RPC (secret must match the `aria2` service) |
| `DOWNLOAD_CONCURRENCY` / `ARIA2_CONNECTIONS` | Parallel tasks / aria2 per-file connections |
| `DOWNLOAD_RETRIES` | Extra retries for transient SSL/network errors |
| `NOTIFY_*` | Group robot webhooks / event toggles |
| `OLLAMA_BASE_URL` / `VLLM_BASE_URL` | Inference endpoints when profiles (or remote servers) are used |
| `VLLM_MODEL` | Model path for the optional `vllm` service |

## Local run (no Docker)

```bash
cp .env.example .env
cd backend
pip install -e ".[dev]"
# point MODEL_ROOT / DATA_DIR / ARIA2_RPC_URL at local paths
uvicorn app.main:app --reload --port 8080
```

The API serves `frontend/` from the repo root (`backend/app/main.py` → `../frontend`). In Docker the image copies that tree to `/frontend` and sets `FRONTEND_DIR=/frontend` so StaticFiles still finds it after `pip install`.
