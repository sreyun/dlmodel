from fastapi import Depends, FastAPI
from app.auth import require_admin


def create_app() -> FastAPI:
    app = FastAPI(title="dlmodel")

    @app.post("/api/auth/verify")
    async def verify(_: None = Depends(require_admin)) -> dict:
        return {"ok": True}

    return app


app = create_app()
