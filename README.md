# dlmodel

Download Hugging Face and ModelScope models into host volumes, with a small web UI and an aria2 sidecar. Optional Compose profiles attach Ollama or vLLM to the same model roots.

## Quick start

1. Copy the example env file and set a real admin token:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and change `ADMIN_TOKEN` from `changeme`.

2. Build and start the default stack (`web` + `aria2`):

   ```bash
   docker compose up -d --build
   ```

3. Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and sign in with `ADMIN_TOKEN`.

## China defaults

The example env is oriented toward mainland China networks:

- `HF_ENDPOINT=https://hf-mirror.com` — Hugging Face Hub traffic goes through the mirror.
- Source `auto` tries ModelScope first, then Hugging Face.
- Downloads use aria2 (`ARIA2_RPC_URL=http://aria2:6800/jsonrpc`) with SDK single-stream fallback if RPC is down.

Set `HF_TOKEN` and/or `MODELSCOPE_API_TOKEN` in `.env` for gated or higher-rate pulls. Tokens can also be saved later in the Settings page.

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
docker compose --profile ollama up -d --build
docker compose --profile vllm up -d --build
docker compose --profile ollama --profile vllm up -d --build
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
| `HF_ENDPOINT` / `HF_TOKEN` | Hugging Face mirror and token |
| `MODELSCOPE_API_TOKEN` | ModelScope token |
| `ARIA2_RPC_URL` / `ARIA2_RPC_SECRET` | aria2 JSON-RPC (secret must match the `aria2` service) |
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
