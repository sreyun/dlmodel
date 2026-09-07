import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.queue import DownloadQueue
from app.routes.auth import router as auth_router
from app.routes.downloads import router as downloads_router
from app.routes.models import router as models_router
from app.routes.services import router as services_router
from app.routes.settings import apply_sqlite_overrides, router as settings_router

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Repo checkout: <root>/backend/app/main.py → parents[2]/frontend.
# Docker: pip install moves __file__ into site-packages; set FRONTEND_DIR=/frontend.
_FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR") or (_REPO_ROOT / "frontend"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db(str(Path(settings.data_dir) / "app.db"))
    await apply_sqlite_overrides()
    settings = get_settings()
    queue = DownloadQueue(concurrency=settings.download_concurrency)
    app.state.queue = queue
    await queue.start()
    try:
        yield
    finally:
        await queue.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(downloads_router)
    app.include_router(models_router)
    app.include_router(services_router)
    # Mount last so "/" does not shadow /api routes.
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="static")
    return app


app = create_app()
