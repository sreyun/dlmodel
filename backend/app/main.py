import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.auth import assert_admin_token_safe
from app.config import get_settings
from app.db import init_db
from app.queue import DownloadQueue
from app.routes.auth import router as auth_router
from app.routes.downloads import router as downloads_router
from app.routes.models import router as models_router
from app.routes.services import router as services_router
from app.routes.settings import apply_sqlite_overrides, router as settings_router

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR") or (_REPO_ROOT / "frontend"))
logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    assert_admin_token_safe()
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    model_root = Path(settings.model_root)
    data_dir.mkdir(parents=True, exist_ok=True)
    model_root.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "app.db"
    await init_db(str(db_path))
    await apply_sqlite_overrides()
    settings = get_settings()
    queue = DownloadQueue(concurrency=settings.download_concurrency)
    app.state.queue = queue
    await queue.start()
    logger.info(
        "dlmodel ready data_dir=%s model_root=%s db=%s",
        data_dir,
        model_root,
        db_path,
    )
    try:
        yield
    finally:
        await queue.stop()
        logger.info("dlmodel shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(settings_router)
    app.include_router(downloads_router)
    app.include_router(models_router)
    app.include_router(services_router)
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="static")
    return app


app = create_app()
