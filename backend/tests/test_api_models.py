import asyncio
import time

from fastapi.testclient import TestClient

from app.main import create_app

HEADERS = {"Authorization": "Bearer secret"}


async def _inflight_run(task, on_progress, on_log, cancelled):
    await on_progress(500, 1000, 10)
    while not cancelled.is_set():
        await asyncio.sleep(0.02)
        await on_progress(500, 1000, 10)


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    return create_app()


def _wait_status(client, tid, *statuses):
    row = None
    for _ in range(60):
        row = client.get(f"/api/downloads/{tid}", headers=HEADERS).json()
        if row.get("status") in statuses:
            return row
        time.sleep(0.05)
    return row


def _model_dir(tmp_path, name):
    path = tmp_path / "models" / "hf" / name
    path.mkdir(parents=True)
    (path / "model.safetensors").write_bytes(b"partial")
    return path


def test_delete_model_rejected_while_download_paused(tmp_path, monkeypatch):
    """暂停的任务还要靠盘上的半成品续传，删除模型前必须先取消任务。"""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        client.app.state.queue._run_download = _inflight_run  # type: ignore[method-assign]
        tid = client.post(
            "/api/downloads",
            headers=HEADERS,
            json={"name": "org/paused", "source": "huggingface", "target": "vllm"},
        ).json()["id"]
        _wait_status(client, tid, "running")
        path = _model_dir(tmp_path, "org/paused")
        assert (
            client.post(f"/api/downloads/{tid}/pause", headers=HEADERS).json()["status"]
            == "paused"
        )

        blocked = client.delete("/api/models/hf/org/paused", headers=HEADERS)
        assert blocked.status_code == 409
        assert path.exists()

        client.post(f"/api/downloads/{tid}/cancel", headers=HEADERS)
        _wait_status(client, tid, "cancelled")
        assert client.delete("/api/models/hf/org/paused", headers=HEADERS).status_code == 200
        assert not path.exists()


def test_delete_model_rejected_while_detached_thread_still_writes(tmp_path, monkeypatch):
    """任务已取消，但 ModelScope 的 SDK 线程还在写目录：不能在它脚下删除。"""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        queue = client.app.state.queue
        path = _model_dir(tmp_path, "org/detached")
        key = queue._dest_key(str(path))
        queue._busy_dests.add(key)
        queue._detached_dests.add(key)

        blocked = client.delete("/api/models/hf/org/detached", headers=HEADERS)
        assert blocked.status_code == 409
        assert path.exists()

        queue._release_dest(key, None)  # 线程结束
        assert client.delete("/api/models/hf/org/detached", headers=HEADERS).status_code == 200
        assert not path.exists()


def test_ollama_pull_is_queued_not_held_open(tmp_path, monkeypatch):
    """拉取走任务队列：请求立刻返回，进度/取消与其他下载一致。"""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        client.app.state.queue._run_download = _inflight_run  # type: ignore[method-assign]
        started = time.monotonic()
        resp = client.post(
            "/api/services/ollama/pull", headers=HEADERS, json={"name": " llama3.2 "}
        )
        assert resp.status_code == 202
        assert time.monotonic() - started < 2
        tid = resp.json()["id"]

        row = _wait_status(client, tid, "running")
        assert (row["name"], row["source"], row["target"]) == ("llama3.2", "ollama", "ollama")

        duplicate = client.post(
            "/api/services/ollama/pull", headers=HEADERS, json={"name": "llama3.2"}
        )
        assert duplicate.status_code == 400

        client.post(f"/api/downloads/{tid}/cancel", headers=HEADERS)
        assert _wait_status(client, tid, "cancelled")["status"] == "cancelled"


def test_ollama_pull_accepts_fully_qualified_reference(tmp_path, monkeypatch):
    """带 registry 前缀或 hf.co 的三段式名称是合法的 Ollama 引用。"""
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        client.app.state.queue._run_download = _inflight_run  # type: ignore[method-assign]
        for name in (
            "registry.ollama.ai/library/llama3.2:latest",
            "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M",
        ):
            resp = client.post(
                "/api/services/ollama/pull", headers=HEADERS, json={"name": name}
            )
            assert resp.status_code == 202, resp.text
            client.post(f"/api/downloads/{resp.json()['id']}/cancel", headers=HEADERS)

        # vLLM 目标仍然只接受 org/repo，名称会落成本地目录。
        vllm = client.post(
            "/api/downloads",
            headers=HEADERS,
            json={"name": "a/b/c", "source": "huggingface", "target": "vllm"},
        )
        assert vllm.status_code == 400


def test_ollama_pull_rejects_bad_name(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        resp = client.post(
            "/api/services/ollama/pull", headers=HEADERS, json={"name": "../etc"}
        )
        assert resp.status_code == 400
