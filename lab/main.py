"""Automation Lab API: one service behind every portfolio build."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .closedesk.api import router as closedesk_router
from .db import migrate, pool
from .platform.api import router as platform_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate()
    yield
    pool().close()


app = FastAPI(title="Automation Lab API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])
app.include_router(platform_router)
app.include_router(closedesk_router)


@app.get("/health")
def health():
    # No database call: Render polls this, and touching Postgres would stop Neon from suspending.
    return {"ok": True}
