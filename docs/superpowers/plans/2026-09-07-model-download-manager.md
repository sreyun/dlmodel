# Model Download Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a FastAPI + static SPA web console that downloads models by name (ModelScope / HF mirror / Ollama), stores them under configurable roots for vLLM or Ollama, accelerates China downloads via aria2 + mirrors, and optionally attaches inference via compose profiles or remote URLs.

**Architecture:** Monolith `web` service (FastAPI serves API + `frontend/`), in-process asyncio task queue with source adapters, aria2 JSON-RPC sidecar with SDK single-stream fallback, SQLite for tasks/settings, optional `ollama` / `vllm` compose profiles sharing model volumes.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, httpx, huggingface_hub, modelscope, aiosqlite, aria2 (sidecar), vanilla JS SPA, Docker Compose.

## Global Constraints

- Auth: shared `ADMIN_TOKEN` Bearer only; no multi-user accounts
- Default `HF_ENDPOINT=https://hf-mirror.com`
- Defaults: `DOWNLOAD_CONCURRENCY=2`, `ARIA2_CONNECTIONS=16`, retries `3`
- Disk layout: `{MODEL_ROOT}/hf/<org>/<repo>/` for vLLM; Ollama via API/volume `{MODEL_ROOT}/ollama`
- aria2 unavailable → SDK single-stream fallback + task log warning
- Do not embed GPU serve inside `web`; vLLM gets launch-command helper
- Management must work without starting ollama/vllm profiles
- YAGNI: no RBAC, no cluster scheduler, no Open WebUI replacement

## File Structure

```text
dlmodel/
  .env.example
  docker-compose.yml
  README.md
  backend/
    pyproject.toml          # or requirements.txt
    Dockerfile
    app/
      __init__.py
      main.py               # FastAPI app, mount static, lifespan
      config.py             # env settings
      auth.py               # Bearer dependency
      db.py                 # aiosqlite schema + helpers
      paths.py              # MODEL_ROOT layout helpers
      models_schema.py      # pydantic request/response
      source_resolve.py     # auto MS→HF / ollama-tag detection
      aria2_client.py       # JSON-RPC wrapper
      downloaders/
        __init__.py
        base.py             # DownloadProgress protocol
        hf.py
        modelscope.py
        ollama.py
        sdk_fallback.py
      queue.py              # asyncio worker pool + task state
      library.py            # scan/delete installed models
      services_ollama.py
      services_vllm.py
      routes/
        auth.py
        settings.py
        downloads.py
        models.py
        services.py
    tests/
      test_paths.py
      test_auth.py
      test_source_resolve.py
      test_aria2_client.py
      test_queue.py
      test_api_downloads.py
      conftest.py
  frontend/
    index.html
    styles.css
    app.js
  docs/superpowers/...
```

---

### Task 1: Scaffold, config, and path helpers

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/paths.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_paths.py`
- Create: `.env.example`

**Interfaces:**
- Produces: `Settings` dataclass/pydantic from env; `hf_model_dir(model_root: str, name: str) -> Path`; `parse_model_name(name: str) -> tuple[str, str]` (`org`, `repo`; if no `/`, org=`library`)

- [ ] **Step 1: Write failing path tests**

```python
# backend/tests/test_paths.py
from pathlib import Path
from app.paths import parse_model_name, hf_model_dir


def test_parse_model_name_with_org():
    assert parse_model_name("Qwen/Qwen2.5-7B-Instruct") == ("Qwen", "Qwen2.5-7B-Instruct")


def test_parse_model_name_without_org():
    assert parse_model_name("llama3.2") == ("library", "llama3.2")


def test_hf_model_dir(tmp_path: Path):
    p = hf_model_dir(str(tmp_path), "Qwen/Qwen2.5-7B-Instruct")
    assert p == tmp_path / "hf" / "Qwen" / "Qwen2.5-7B-Instruct"
```

- [ ] **Step 2: Run tests — expect fail**

Run: `cd backend && pip install pytest pydantic-settings && pytest tests/test_paths.py -v`  
Expected: FAIL (`ModuleNotFoundError` or import error)

- [ ] **Step 3: Implement config + paths + pyproject**

```toml
# backend/pyproject.toml
[project]
name = "dlmodel"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115.0",
  "uvicorn[standard]>=0.30.0",
  "httpx>=0.27.0",
  "pydantic-settings>=2.4.0",
  "aiosqlite>=0.20.0",
  "huggingface_hub>=0.24.0",
  "modelscope>=1.18.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
```

```python
# backend/app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_token: str = "changeme"
    model_root: str = "/models"
    data_dir: str = "/data"
    hf_endpoint: str = "https://hf-mirror.com"
    hf_token: str | None = None
    modelscope_api_token: str | None = None
    aria2_rpc_url: str = "http://aria2:6800/jsonrpc"
    aria2_rpc_secret: str = "dlmodel"
    download_concurrency: int = 2
    aria2_connections: int = 16
    download_retries: int = 3
    ollama_base_url: str = "http://ollama:11434"
    vllm_base_url: str = "http://vllm:8000"
    vllm_model: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080


def get_settings() -> Settings:
    return Settings()
```

```python
# backend/app/paths.py
from pathlib import Path


def parse_model_name(name: str) -> tuple[str, str]:
    name = name.strip().strip("/")
    if "/" in name:
        org, repo = name.split("/", 1)
        return org, repo
    return "library", name


def hf_model_dir(model_root: str, name: str) -> Path:
    org, repo = parse_model_name(name)
    return Path(model_root) / "hf" / org / repo


def ollama_root(model_root: str) -> Path:
    return Path(model_root) / "ollama"
```

```dotenv
# .env.example
ADMIN_TOKEN=changeme
MODEL_ROOT=/models
DATA_DIR=/data
HF_ENDPOINT=https://hf-mirror.com
HF_TOKEN=
MODELSCOPE_API_TOKEN=
ARIA2_RPC_URL=http://aria2:6800/jsonrpc
ARIA2_RPC_SECRET=dlmodel
DOWNLOAD_CONCURRENCY=2
ARIA2_CONNECTIONS=16
OLLAMA_BASE_URL=http://ollama:11434
VLLM_BASE_URL=http://vllm:8000
```

- [ ] **Step 4: Run tests — expect pass**

Run: `cd backend && pip install -e ".[dev]" && pytest tests/test_paths.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend .env.example
git commit -m "feat: scaffold backend config and HF path helpers"
```

---

### Task 2: Bearer auth dependency

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/tests/test_auth.py`
- Create: `backend/app/main.py` (minimal app for TestClient)

**Interfaces:**
- Consumes: `get_settings().admin_token`
- Produces: `async def require_admin(authorization: Annotated[str | None, Header()] = None) -> None` raising `HTTPException(401)`

- [ ] **Step 1: Write failing auth tests**

```python
# backend/tests/test_auth.py
import os
from fastapi.testclient import TestClient


def test_verify_ok(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app.main import create_app
    client = TestClient(create_app())
    r = client.post("/api/auth/verify", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_verify_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    from app.main import create_app
    client = TestClient(create_app())
    r = client.post("/api/auth/verify", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_auth.py -v`  
Expected: FAIL (missing create_app / routes)

- [ ] **Step 3: Implement auth + minimal main**

```python
# backend/app/auth.py
from typing import Annotated
from fastapi import Header, HTTPException
from app.config import get_settings


async def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != get_settings().admin_token:
        raise HTTPException(status_code=401, detail="Invalid token")
```

```python
# backend/app/main.py
from fastapi import Depends, FastAPI
from app.auth import require_admin


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel")

    @app.post("/api/auth/verify")
    async def verify(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    return app


app = create_app()
```

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_auth.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/auth.py backend/app/main.py backend/tests/test_auth.py
git commit -m "feat: add Bearer ADMIN_TOKEN auth gate"
```

---

### Task 3: SQLite DB for settings overrides and download tasks

**Files:**
- Create: `backend/app/db.py`
- Create: `backend/app/models_schema.py`
- Create: `backend/tests/test_db.py`
- Modify: `backend/app/main.py` (lifespan init_db)

**Interfaces:**
- Produces:
  - `async def init_db(db_path: str) -> None`
  - `async def get_setting(key: str) -> str | None` / `async def set_setting(key: str, value: str) -> None`
  - `async def insert_task(task: dict) -> str` / `async def update_task(task_id: str, **fields) -> None` / `async def list_tasks() -> list[dict]` / `async def get_task(task_id: str) -> dict | None`
- Task row fields: `id`, `name`, `source`, `target`, `revision`, `status` (`queued|running|completed|failed|cancelled`), `progress_bytes`, `total_bytes`, `speed_bps`, `message`, `dest_path`, `created_at`, `updated_at`

- [ ] **Step 1: Write failing db tests**

```python
# backend/tests/test_db.py
import pytest
from app.db import init_db, set_setting, get_setting, insert_task, get_task, update_task


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "app.db")
    await init_db(path)
    return path


@pytest.mark.asyncio
async def test_settings_roundtrip(db, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(db).rsplit("/", 1)[0] if False else "")
    # use module-level connection helper: set DATA_DIR via init already
    await set_setting("hf_endpoint", "https://hf-mirror.com")
    assert await get_setting("hf_endpoint") == "https://hf-mirror.com"


@pytest.mark.asyncio
async def test_task_insert_and_update(db):
    tid = await insert_task({
        "name": "Qwen/Qwen2.5-0.5B",
        "source": "auto",
        "target": "vllm",
        "revision": None,
        "status": "queued",
        "dest_path": "/models/hf/Qwen/Qwen2.5-0.5B",
    })
    await update_task(tid, status="running", progress_bytes=10, total_bytes=100)
    row = await get_task(tid)
    assert row["status"] == "running"
    assert row["progress_bytes"] == 10
```

Implement `db.py` so tests do not need monkeypatch hacks: `init_db(path)` stores path in module global `_DB_PATH`.

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_db.py -v`  
Expected: FAIL

- [ ] **Step 3: Implement `db.py` + pydantic schemas**

```python
# backend/app/models_schema.py
from typing import Literal
from pydantic import BaseModel, Field


Source = Literal["auto", "modelscope", "huggingface", "ollama"]
Target = Literal["vllm", "ollama"]
TaskStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class DownloadCreate(BaseModel):
    name: str = Field(min_length=1)
    source: Source = "auto"
    target: Target = "vllm"
    revision: str | None = None


class TaskOut(BaseModel):
    id: str
    name: str
    source: str
    target: str
    revision: str | None
    status: TaskStatus
    progress_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float | None = None
    message: str = ""
    dest_path: str = ""
    created_at: str
    updated_at: str
```

Implement `backend/app/db.py` with aiosqlite: tables `settings(key TEXT PRIMARY KEY, value TEXT)` and `tasks(...)` as listed in Interfaces. Use `uuid4().hex` for ids; ISO timestamps via `datetime.utcnow().isoformat()`.

Wire `lifespan` in `create_app` to `await init_db(str(Path(settings.data_dir) / "app.db"))`.

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_db.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/db.py backend/app/models_schema.py backend/tests/test_db.py backend/app/main.py
git commit -m "feat: add SQLite persistence for settings and download tasks"
```

---

### Task 4: Source resolution (`auto` MS→HF, Ollama tag)

**Files:**
- Create: `backend/app/source_resolve.py`
- Create: `backend/tests/test_source_resolve.py`

**Interfaces:**
- Produces: `def looks_like_ollama_library(name: str) -> bool` — True if no `/` or name matches `^[a-z0-9._-]+(:[a-z0-9._-]+)?$` without org slash for common ollama tags
- Produces: `async def resolve_source(name: str, source: str, target: str, *, ms_exists: callable, hf_exists: callable) -> list[str]` returning ordered adapter keys to try, e.g. `["modelscope", "huggingface"]` or `["ollama"]`
- Inject `ms_exists`/`hf_exists` for tests (async callables `(name) -> bool`)

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_source_resolve.py
import pytest
from app.source_resolve import looks_like_ollama_library, resolve_source


def test_ollama_library_tag():
    assert looks_like_ollama_library("llama3.2")
    assert looks_like_ollama_library("llama3.2:latest")
    assert not looks_like_ollama_library("Qwen/Qwen2.5-7B")


@pytest.mark.asyncio
async def test_auto_prefers_modelscope_then_hf():
    async def ms_ok(n): return True
    async def hf_ok(n): return True
    order = await resolve_source("Qwen/Qwen2.5", "auto", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["modelscope"]


@pytest.mark.asyncio
async def test_auto_falls_back_to_hf():
    async def ms_ok(n): return False
    async def hf_ok(n): return True
    order = await resolve_source("Qwen/Qwen2.5", "auto", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["huggingface"]


@pytest.mark.asyncio
async def test_target_ollama_library_uses_ollama():
    async def ms_ok(n): return False
    async def hf_ok(n): return False
    order = await resolve_source("llama3.2", "auto", "ollama", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["ollama"]


@pytest.mark.asyncio
async def test_forced_source():
    async def ms_ok(n): return False
    async def hf_ok(n): return False
    order = await resolve_source("x/y", "huggingface", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["huggingface"]
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_source_resolve.py -v`

- [ ] **Step 3: Implement `source_resolve.py`**

Logic:
- If `source != "auto"`: return `[source]`
- If `target == "ollama"` and `looks_like_ollama_library(name)`: return `["ollama"]`
- Else probe MS then HF; return first that exists; if none exist return `["modelscope", "huggingface"]` so runtime still tries and fails with clear errors
- For auto when MS exists: return only `["modelscope"]` (no need to try HF)

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_source_resolve.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/source_resolve.py backend/tests/test_source_resolve.py
git commit -m "feat: resolve download source with MS→HF auto fallback"
```

---

### Task 5: aria2 JSON-RPC client + SDK fallback downloader

**Files:**
- Create: `backend/app/aria2_client.py`
- Create: `backend/app/downloaders/base.py`
- Create: `backend/app/downloaders/sdk_fallback.py`
- Create: `backend/tests/test_aria2_client.py`

**Interfaces:**
- Produces: `class Aria2Client` with `async def is_available() -> bool`, `async def add_uri(uris: list[str], out_dir: str, out_name: str, connections: int) -> str` (gid), `async def tell_status(gid: str) -> dict` with keys `completed_length`, `total_length`, `download_speed`, `status`
- Produces: `async def http_download(url: str, dest: Path, on_progress) -> None` in sdk_fallback (httpx stream)
- Produces: `ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]`  # downloaded, total, speed

- [ ] **Step 1: Write failing aria2 unit test with httpx mock**

```python
# backend/tests/test_aria2_client.py
import pytest
import respx
from httpx import Response
from app.aria2_client import Aria2Client


@pytest.mark.asyncio
@respx.mock
async def test_add_uri_and_status():
    route = respx.post("http://aria2.test/jsonrpc").mock(side_effect=[
        Response(200, json={"id": "1", "jsonrpc": "2.0", "result": "gid-1"}),
        Response(200, json={"id": "2", "jsonrpc": "2.0", "result": {
            "status": "complete",
            "completedLength": "100",
            "totalLength": "100",
            "downloadSpeed": "0",
        }}),
    ])
    client = Aria2Client("http://aria2.test/jsonrpc", secret="token")
    gid = await client.add_uri(["https://example.com/f.bin"], "/tmp", "f.bin", 16)
    assert gid == "gid-1"
    st = await client.tell_status(gid)
    assert st["status"] == "complete"
    assert st["completed_length"] == 100
    assert route.call_count == 2
```

Add `respx` to optional-dependencies in `pyproject.toml`.

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pip install respx && pytest tests/test_aria2_client.py -v`

- [ ] **Step 3: Implement Aria2Client (JSON-RPC 2.0)**

Methods use body `{"jsonrpc":"2.0","id":"...","method":"aria2.addUri","params":["token:SECRET", uris, {"dir":..., "out":..., "split": N, "max-connection-per-server": N, "continue": "true"}]}`.  
`aria2.tellStatus` similarly.  
`is_available` calls `aria2.getVersion` and returns False on any error.

Implement minimal `sdk_fallback.http_download` writing to `dest` with progress callbacks.

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_aria2_client.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/aria2_client.py backend/app/downloaders backend/tests/test_aria2_client.py backend/pyproject.toml
git commit -m "feat: add aria2 RPC client and HTTP download fallback"
```

---

### Task 6: HF / ModelScope / Ollama download adapters

**Files:**
- Create: `backend/app/downloaders/hf.py`
- Create: `backend/app/downloaders/modelscope.py`
- Create: `backend/app/downloaders/ollama.py`
- Create: `backend/app/downloaders/__init__.py`
- Create: `backend/tests/test_downloaders_unit.py`

**Interfaces:**
- Produces:
  - `async def hf_repo_exists(name: str, endpoint: str, token: str | None) -> bool`
  - `async def download_hf(name: str, dest: Path, *, endpoint, token, revision, aria2: Aria2Client | None, connections: int, on_progress, on_log) -> None`
  - `async def ms_repo_exists(name: str, token: str | None) -> bool`
  - `async def download_modelscope(name: str, dest: Path, *, token, revision, on_progress, on_log) -> None`
  - `async def download_ollama(name: str, base_url: str, on_progress, on_log) -> None` — POST `{base}/api/pull` stream JSON lines
- HF strategy: list files via `HfApi(endpoint=...).list_repo_files`; for each file get download URL; if aria2 available use it else `hf_hub_download` / httpx fallback; assemble into `dest`
- ModelScope: use `snapshot_download` in a worker thread (`asyncio.to_thread`), approximate progress via on_log messages if SDK lacks fine progress
- Ollama: stream pull API; parse `total`/`completed` from JSON lines for progress

- [ ] **Step 1: Write unit tests with mocks**

```python
# backend/tests/test_downloaders_unit.py
import pytest
from unittest.mock import AsyncMock, patch
from app.downloaders.ollama import download_ollama


@pytest.mark.asyncio
async def test_ollama_pull_parses_progress(respx_mock=None):
    import httpx
    import respx
    from httpx import Response

    async def gen():
        yield b'{"status":"pulling","total":100,"completed":40}\n'
        yield b'{"status":"success"}\n'

    with respx.mock:
        respx.post("http://ollama/api/pull").mock(return_value=Response(200, content=b'{"status":"pulling","total":100,"completed":40}\n{"status":"success"}\n'))
        progresses = []
        async def on_progress(d, t, s):
            progresses.append((d, t))
        async def on_log(m):
            pass
        await download_ollama("llama3.2", "http://ollama", on_progress=on_progress, on_log=on_log)
        assert any(p[0] == 40 and p[1] == 100 for p in progresses)
```

Also test `hf_repo_exists` with mocked `HfApi.repo_info` raising vs succeeding (patch).

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_downloaders_unit.py -v`

- [ ] **Step 3: Implement the three adapters**

Set `os.environ["HF_ENDPOINT"]` from settings inside download_hf before hub calls.  
On aria2 failure mid-file, log warning and finish that file via sdk_fallback.

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_downloaders_unit.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/downloaders backend/tests/test_downloaders_unit.py
git commit -m "feat: add HF, ModelScope, and Ollama download adapters"
```

---

### Task 7: Download task queue / worker pool

**Files:**
- Create: `backend/app/queue.py`
- Create: `backend/tests/test_queue.py`
- Modify: `backend/app/main.py` (start/stop workers in lifespan)

**Interfaces:**
- Produces: `class DownloadQueue`:
  - `async def start(self) -> None` / `async def stop(self) -> None`
  - `async def enqueue(self, payload: DownloadCreate) -> str` — inserts DB row `queued`, returns id
  - `async def cancel(self, task_id: str) -> None`
  - `async def retry(self, task_id: str) -> None` — requeue failed/cancelled
  - `def subscribe(self, task_id: str) -> asyncio.Queue` — progress event dicts `{progress_bytes,total_bytes,speed_bps,status,message}`
- Worker count = `settings.download_concurrency`
- Run pipeline: resolve_source → try adapters in order → update DB → publish events
- Cancel: set flag checked between files / poll aria2 remove

- [ ] **Step 1: Write failing queue test with fake adapters**

```python
# backend/tests/test_queue.py
import pytest
from pathlib import Path
from app.db import init_db
from app.queue import DownloadQueue
from app.models_schema import DownloadCreate


@pytest.mark.asyncio
async def test_enqueue_runs_to_completion(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    q = DownloadQueue(concurrency=1)
    # inject fake runner
    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(50, 100, 1.0)
        dest = Path(task["dest_path"])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ok").write_text("1")
        await on_progress(100, 100, 0)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    # wait until completed
    for _ in range(50):
        from app.db import get_task
        row = await get_task(tid)
        if row["status"] in ("completed", "failed"):
            break
        import asyncio
        await asyncio.sleep(0.05)
    await q.stop()
    row = await get_task(tid)
    assert row["status"] == "completed"
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_queue.py -v`

- [ ] **Step 3: Implement `DownloadQueue`**

Compute `dest_path` via `hf_model_dir` for vllm target; for ollama target use empty dest or ollama root note in message.  
On unknown model after all sources: `status=failed`, `message="Model not found on attempted sources: ..."`.

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_queue.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue.py backend/tests/test_queue.py backend/app/main.py
git commit -m "feat: add concurrent download task queue with progress events"
```

---

### Task 8: REST routes — downloads, settings, SSE

**Files:**
- Create: `backend/app/routes/__init__.py`
- Create: `backend/app/routes/auth.py`
- Create: `backend/app/routes/settings.py`
- Create: `backend/app/routes/downloads.py`
- Create: `backend/tests/test_api_downloads.py`
- Modify: `backend/app/main.py` — include routers; attach `app.state.queue`

**Interfaces:**
- `POST /api/auth/verify`
- `GET /api/settings` / `PUT /api/settings` (persist overrides in SQLite; merge over env defaults for: hf_endpoint, tokens, concurrency, aria2_connections, ollama_base_url, vllm_base_url)
- `POST /api/downloads` → `{id}`
- `GET /api/downloads` / `GET /api/downloads/{id}`
- `POST /api/downloads/{id}/cancel` / `POST /api/downloads/{id}/retry`
- `GET /api/downloads/{id}/events` — `StreamingResponse` text/event-stream

- [ ] **Step 1: Write API integration test**

```python
# backend/tests/test_api_downloads.py
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_create_download_requires_auth(client):
    r = client.post("/api/downloads", json={"name": "a/b", "source": "huggingface", "target": "vllm"})
    assert r.status_code == 401


def test_create_download_ok(client, monkeypatch):
    # patch queue enqueue to no-op real download: already fake in test by short-circuit if needed
    headers = {"Authorization": "Bearer secret"}
    r = client.post("/api/downloads", headers=headers, json={"name": "a/b", "source": "huggingface", "target": "vllm"})
    assert r.status_code == 200
    assert "id" in r.json()
    tid = r.json()["id"]
    r2 = client.get(f"/api/downloads/{tid}", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["name"] == "a/b"
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_api_downloads.py -v`

- [ ] **Step 3: Implement routers and wire main**

SSE loop: subscribe to queue events; `yield f"data: {json.dumps(evt)}\n\n"`; end when status in terminal set.

Settings PUT body example: `{"hf_endpoint":"https://hf-mirror.com","download_concurrency":2}`.

- [ ] **Step 4: Run all backend tests**

Run: `cd backend && pytest -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes backend/app/main.py backend/tests/test_api_downloads.py
git commit -m "feat: expose downloads, settings, and SSE progress APIs"
```

---

### Task 9: Model library scan & delete

**Files:**
- Create: `backend/app/library.py`
- Create: `backend/app/routes/models.py`
- Create: `backend/tests/test_library.py`
- Modify: `backend/app/main.py` include models router

**Interfaces:**
- `def scan_hf_library(model_root: str) -> list[dict]` — each `{id, name, path, target:"vllm", size_bytes}`
- `def delete_model(model_root: str, model_id: str) -> None` — `model_id` like `hf/Qwen/Qwen2.5-7B-Instruct`; `shutil.rmtree` under model_root only (reject path traversal)
- Routes: `GET /api/models`, `DELETE /api/models/{model_id:path}`

- [ ] **Step 1: Write failing library tests**

```python
# backend/tests/test_library.py
from pathlib import Path
from app.library import scan_hf_library, delete_model
import pytest


def test_scan_and_delete(tmp_path: Path):
    dest = tmp_path / "hf" / "Org" / "Mod"
    dest.mkdir(parents=True)
    (dest / "config.json").write_text("{}")
    items = scan_hf_library(str(tmp_path))
    assert len(items) == 1
    assert items[0]["name"] == "Org/Mod"
    delete_model(str(tmp_path), items[0]["id"])
    assert scan_hf_library(str(tmp_path)) == []


def test_delete_rejects_traversal(tmp_path: Path):
    with pytest.raises(ValueError):
        delete_model(str(tmp_path), "../etc/passwd")
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_library.py -v`

- [ ] **Step 3: Implement library + route**

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_library.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/library.py backend/app/routes/models.py backend/tests/test_library.py backend/app/main.py
git commit -m "feat: scan and delete installed HF-layout models"
```

---

### Task 10: Ollama / vLLM service helpers

**Files:**
- Create: `backend/app/services_ollama.py`
- Create: `backend/app/services_vllm.py`
- Create: `backend/app/routes/services.py`
- Create: `backend/tests/test_services.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- `async def ollama_health(base_url: str) -> dict` → `{ok: bool, detail: str}`
- `async def ollama_list_models(base_url: str) -> list[dict]`
- `async def ollama_pull(base_url: str, name: str) -> None` (or enqueue reuse)
- `async def vllm_health(base_url: str) -> dict` — GET `{base}/health` or `/v1/models`
- `def vllm_launch_command(model_path: str, port: int = 8000) -> str` → `python -m vllm.entrypoints.openai.api_server --model {model_path} --port {port}`

- [ ] **Step 1: Write tests with respx**

```python
# backend/tests/test_services.py
import pytest
import respx
from httpx import Response
from app.services_ollama import ollama_health
from app.services_vllm import vllm_launch_command


@pytest.mark.asyncio
@respx.mock
async def test_ollama_health_ok():
    respx.get("http://ollama:11434/api/tags").mock(return_value=Response(200, json={"models": []}))
    h = await ollama_health("http://ollama:11434")
    assert h["ok"] is True


def test_vllm_launch_command():
    cmd = vllm_launch_command("/models/hf/Qwen/Qwen2.5-7B-Instruct")
    assert "--model /models/hf/Qwen/Qwen2.5-7B-Instruct" in cmd
```

- [ ] **Step 2: Run — expect fail**

Run: `cd backend && pytest tests/test_services.py -v`

- [ ] **Step 3: Implement services + routes**

Routes from spec section 8.

- [ ] **Step 4: Run — expect pass**

Run: `cd backend && pytest tests/test_services.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/app/services_ollama.py backend/app/services_vllm.py backend/app/routes/services.py backend/tests/test_services.py backend/app/main.py
git commit -m "feat: add Ollama/vLLM health and launch-command helpers"
```

---

### Task 11: Frontend SPA (5 pages)

**Files:**
- Create: `frontend/index.html`
- Create: `frontend/styles.css`
- Create: `frontend/app.js`
- Modify: `backend/app/main.py` — `app.mount("/", StaticFiles(directory=frontend, html=True), name="static")` after API routes

**Interfaces:**
- Consumes all APIs above; stores token in `localStorage.dlmodel_token`
- Pages via hash routes: `#/download` `#/tasks` `#/library` `#/services` `#/settings`

- [ ] **Step 1: Build HTML shell with nav + login gate**

`index.html`: brand title `dlmodel`, nav links, `<main id="app">`, script `app.js`.

- [ ] **Step 2: Implement `app.js` API helper**

```javascript
async function api(path, opts = {}) {
  const token = localStorage.getItem("dlmodel_token") || "";
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {}, {
    Authorization: `Bearer ${token}`,
  });
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) throw new Error("unauthorized");
  return res;
}
```

Implement views:
- Login prompt if verify fails
- Download form → POST `/api/downloads`
- Tasks table + EventSource to `/api/downloads/{id}/events` (pass token via query `?token=` **or** use fetch stream — prefer polling GET every 1s if EventSource cannot set Authorization; **v1 decision: poll GET /api/downloads every 1s on Tasks page**, keep SSE endpoint for optional use with `?token=` query supported by backend)
- Library list + delete
- Services health + copy launch command
- Settings form GET/PUT

- [ ] **Step 3: Add backend support for SSE token query fallback**

In downloads events route: accept `token` query if Authorization header missing; validate against `admin_token`.

- [ ] **Step 4: Manual browser check locally**

Run: `cd backend && DATA_DIR=/tmp/dlmodel MODEL_ROOT=/tmp/models ADMIN_TOKEN=secret uvicorn app.main:app --reload --port 8080`  
Open `http://127.0.0.1:8080`, login, open each hash page.  
Expected: pages render; unauthorized API blocked.

- [ ] **Step 5: Commit**

```bash
git add frontend backend/app/main.py backend/app/routes/downloads.py
git commit -m "feat: add static SPA for download, tasks, library, services, settings"
```

---

### Task 12: Docker Compose, Dockerfile, README

**Files:**
- Create: `backend/Dockerfile`
- Create: `docker-compose.yml`
- Create: `README.md`
- Modify: `.env.example` if needed

**Interfaces:**
- Services: `web`, `aria2`; profiles `ollama`, `vllm`
- Volumes: `model-data` or bind `./data/models:/models`, `./data/app:/data`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY backend/pyproject.toml /app/
COPY backend/app /app/app
COPY frontend /frontend
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data MODEL_ROOT=/models
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: Write docker-compose.yml**

```yaml
services:
  web:
    build:
      context: .
      dockerfile: backend/Dockerfile
    ports: ["8080:8080"]
    env_file: .env
    volumes:
      - ./data/models:/models
      - ./data/app:/data
    depends_on: [aria2]
  aria2:
    image: p3terx/aria2-pro:latest
    environment:
      RPC_SECRET: ${ARIA2_RPC_SECRET:-dlmodel}
      RPC_PORT: 6800
    volumes:
      - ./data/models:/models
      - ./data/aria2:/config
  ollama:
    profiles: ["ollama"]
    image: ollama/ollama:latest
    ports: ["11434:11434"]
    volumes:
      - ./data/models/ollama:/root/.ollama
  vllm:
    profiles: ["vllm"]
    image: vllm/vllm-openai:latest
    runtime: nvidia
    ports: ["8000:8000"]
    environment:
      HUGGING_FACE_HUB_TOKEN: ${HF_TOKEN:-}
    volumes:
      - ./data/models/hf:/models/hf
    command: ["--model", "${VLLM_MODEL:-/models/hf}", "--host", "0.0.0.0", "--port", "8000"]
```

Note in README: adjust `vllm` image/command for host GPU; `VLLM_MODEL` must point to a concrete downloaded repo path.

- [ ] **Step 3: Write README**

Cover: copy `.env.example` → `.env`, set `ADMIN_TOKEN`, `docker compose up -d --build`, open `:8080`, China defaults, `--profile ollama` / `--profile vllm`, model roots, HF/MS tokens.

- [ ] **Step 4: Compose config validate**

Run: `docker compose config`  
Expected: valid YAML, profiles present

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile docker-compose.yml README.md .env.example
git commit -m "feat: add docker-compose stack with aria2 and optional ollama/vllm"
```

---

### Task 13: End-to-end smoke + final verification

**Files:**
- Modify: none required unless bugs found

- [ ] **Step 1: Run full pytest**

Run: `cd backend && pytest -v`  
Expected: all PASS

- [ ] **Step 2: Start compose (web+aria2 only)**

Run: `docker compose up -d --build`  
Expected: web healthy on 8080; aria2 up

- [ ] **Step 3: Smoke download of tiny model via API**

```bash
curl -s -X POST http://127.0.0.1:8080/api/downloads \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"hf-internal-testing/tiny-random-gpt2\",\"source\":\"huggingface\",\"target\":\"vllm\"}"
```

Poll task until `completed` or `failed`; if network blocks HF mirror in CI environment, document manual verification instead and still ensure API accepts job.

- [ ] **Step 4: Fix any smoke failures**

- [ ] **Step 5: Final commit if fixes landed**

```bash
git add -A
git commit -m "fix: address compose smoke issues"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|------------------|------|
| Download by name + source/target | 6, 7, 8, 11 |
| ModelScope + HF + Ollama | 4, 6 |
| MS→HF auto + ollama library detection | 4 |
| HF mirror default + tokens | 1, 8, 12 |
| aria2 + SDK fallback | 5, 6 |
| Queue concurrency / cancel / retry / progress | 7, 8 |
| Disk layout hf/ollama | 1, 7, 9 |
| ADMIN_TOKEN auth | 2, 11 |
| Settings page / persistence | 3, 8, 11 |
| Library list/delete | 9, 11 |
| Ollama/vLLM health + launch command | 10, 11 |
| Compose profiles separable | 12 |
| SPA 5 pages + SSE/poll | 11 |
| Tests unit/integration | 1–10, 13 |

## Placeholder / consistency notes

- Progress transport: Tasks page uses **1s polling**; SSE kept with `?token=` for optional live view — consistent across Task 8 and 11.
- Settings field names use snake_case matching `Settings` / SQLite keys.
- Queue method names: `enqueue`, `cancel`, `retry`, `subscribe`, `start`, `stop`.
