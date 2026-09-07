from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI

from app.auth import require_admin
from app.config import get_settings
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db(str(Path(settings.data_dir) / "app.db"))
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel", lifespan=lifespan)

    @app.post("/api/auth/verify")
    async def verify(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    return app


app = create_app()
