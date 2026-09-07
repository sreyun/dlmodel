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
