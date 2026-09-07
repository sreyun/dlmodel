from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.routes.settings import effective_settings
from app.services_ollama import ollama_health, ollama_list_models, ollama_pull
from app.services_vllm import vllm_health, vllm_launch_command

router = APIRouter()


class OllamaPullRequest(BaseModel):
    name: str = Field(min_length=1)


@router.get("/api/services/ollama/health")
async def ollama_health_route(_: None = Depends(require_admin)) -> dict:
    settings = await effective_settings()
    return await ollama_health(settings["ollama_base_url"])


@router.get("/api/services/ollama/models")
async def ollama_models_route(_: None = Depends(require_admin)) -> list[dict]:
    settings = await effective_settings()
    try:
        return await ollama_list_models(settings["ollama_base_url"])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/services/ollama/pull")
async def ollama_pull_route(
    body: OllamaPullRequest, _: None = Depends(require_admin)
) -> dict:
    settings = await effective_settings()
    try:
        await ollama_pull(settings["ollama_base_url"], body.name.strip())
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/api/services/vllm/health")
async def vllm_health_route(_: None = Depends(require_admin)) -> dict:
    settings = await effective_settings()
    return await vllm_health(settings["vllm_base_url"])


@router.get("/api/services/vllm/launch-command")
async def vllm_launch_command_route(
    model: str = Query(..., min_length=1),
    port: int = Query(8000, ge=1, le=65535),
    _: None = Depends(require_admin),
) -> dict:
    try:
        return {"command": vllm_launch_command(model, port)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
