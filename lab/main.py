"""Automation Lab API: one service behind every portfolio build."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .closedesk import repo as closedesk_repo
from .closedesk.api import labels as closedesk_labels
from .closedesk.api import router as closedesk_router
from .askfinance.api import router as askfinance_router
from .exceptions.api import router as exceptions_router
from .db import migrate, pool
from .platform.api import router as platform_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()
    try:
        with pool().connection() as conn:
            closedesk_repo.seed_history(conn, closedesk_labels())
    except FileNotFoundError:      # labels.json not generated yet (local dev)
        pass
    yield
    pool().close()


app = FastAPI(title="Automation Lab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])
app.include_router(platform_router)
app.include_router(closedesk_router)
app.include_router(askfinance_router)
app.include_router(exceptions_router)


@app.get("/health")
def health():
    # No database call: Render polls this, and touching Postgres would stop Neon from suspending.
    return {"ok": True}


WEB = Path(__file__).resolve().parent / "web"


@app.get("/", include_in_schema=False)
@app.get("/closedesk", include_in_schema=False)
def closedesk_page():
    return FileResponse(WEB / "closedesk.html")


@app.get("/askfinance", include_in_schema=False)
def askfinance_page():
    return FileResponse(WEB / "askfinance.html")


@app.get("/exceptions", include_in_schema=False)
def exceptions_page():
    return FileResponse(WEB / "exceptions.html")
