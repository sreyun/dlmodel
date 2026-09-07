from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI

from app.auth import require_admin
from app.config import get_settings
from app.db import init_db
from app.queue import DownloadQueue


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db(str(Path(settings.data_dir) / "app.db"))
    queue = DownloadQueue(concurrency=settings.download_concurrency)
    app.state.queue = queue
    await queue.start()
    try:
        yield
    finally:
        await queue.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel", lifespan=lifespan)

    @app.post("/api/auth/verify")
    async def verify(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    return app


app = create_app()
