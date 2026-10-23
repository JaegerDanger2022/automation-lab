"""Shared platform endpoints. n8n calls these from every build.

Write endpoints need the X-Lab-Key header. Read endpoints are public so the demo
site can show a run's timeline and the overall status page.
"""
from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..db import pool

router = APIRouter(prefix="/v1", tags=["platform"])


def require_key(x_lab_key: str | None = Header(default=None)):
    expected = os.environ.get("LAB_API_KEY")
    if not expected or x_lab_key != expected:
        raise HTTPException(status_code=401, detail="Missing or wrong X-Lab-Key")


def hash_visitor(raw: str | None) -> str | None:
    if not raw:
        return None
    salt = os.environ.get("VISITOR_SALT", "lab")
    return hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()[:24]


class StartRun(BaseModel):
    build: str
    is_demo: bool = False
    input_ref: str | None = None
    visitor: str | None = None          # raw IP or phone; hashed before storage
    idempotency_key: str | None = None
    meta: dict[str, Any] = {}


class Step(BaseModel):
    name: str
    status: str = "ok"
    duration_ms: int | None = None
    detail: dict[str, Any] = {}


class FinishRun(BaseModel):
    status: str                          # succeeded | failed | held
    cost_usd: Decimal = Decimal("0")
    provider: str | None = None          # for the spend ledger, e.g. anthropic
    error: str | None = None


def _setting(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def _bump(conn, scope: str) -> int:
    return conn.execute(
        "INSERT INTO usage_counters (scope, day, count) VALUES (%s, current_date, 1) "
        "ON CONFLICT (scope, day) DO UPDATE SET count = usage_counters.count + 1 RETURNING count",
        (scope,)).fetchone()[0]


@router.post("/runs", dependencies=[Depends(require_key)])
def start_run(body: StartRun):
    """Opens a run. Refuses duplicates, paused demos, spent budgets and visitors over their limit."""
    with pool().connection() as conn:
        if body.idempotency_key:
            row = conn.execute("SELECT run_id FROM idempotency_keys WHERE key = %s", (body.idempotency_key,)).fetchone()
            if row:
                return {"duplicate": True, "run_id": str(row[0]) if row[0] else None}
        visitor = hash_visitor(body.visitor)
        if body.is_demo:
            if _setting(conn, "demos_paused", False):
                raise HTTPException(503, "Demos are paused for today. Watch the recorded walkthrough instead.")
            cap = Decimal(str(_setting(conn, "monthly_spend_cap_usd", 10)))
            spent = conn.execute("SELECT coalesce(sum(usd), 0) FROM spend_ledger "
                                 "WHERE day >= date_trunc('month', current_date)").fetchone()[0]
            if spent >= cap * Decimal("0.9"):
                conn.execute("UPDATE settings SET value = 'true'::jsonb WHERE key = 'demos_paused'")
                raise HTTPException(503, "Monthly demo budget reached. Demos resume next month.")
            limits = _setting(conn, "limits", {})
            if _bump(conn, f"demo:{body.build}") > limits.get("per_demo_per_day", 200):
                raise HTTPException(429, "This demo has hit its daily limit. Try again tomorrow.")
            if visitor and _bump(conn, f"visitor:{visitor}:{body.build}") > limits.get("per_visitor_per_demo_per_day", 5):
                raise HTTPException(429, "You've used this demo 5 times today. Try again tomorrow.")
        run_id = conn.execute(
            "INSERT INTO runs (build, is_demo, input_ref, visitor_hash, meta) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (body.build, body.is_demo, body.input_ref, visitor, Jsonb(body.meta))).fetchone()[0]
        if body.idempotency_key:
            conn.execute("INSERT INTO idempotency_keys (key, build, run_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (body.idempotency_key, body.build, run_id))
        return {"duplicate": False, "run_id": str(run_id)}


@router.post("/runs/{run_id}/steps", dependencies=[Depends(require_key)])
def add_step(run_id: str, body: Step):
    with pool().connection() as conn:
        conn.execute("INSERT INTO run_steps (run_id, name, status, duration_ms, detail) VALUES (%s,%s,%s,%s,%s)",
                     (run_id, body.name, body.status, body.duration_ms, Jsonb(body.detail)))
    return {"ok": True}


@router.post("/runs/{run_id}/finish", dependencies=[Depends(require_key)])
def finish_run(run_id: str, body: FinishRun):
    with pool().connection() as conn:
        conn.execute("UPDATE runs SET status = %s, finished_at = now(), cost_usd = cost_usd + %s, error = %s "
                     "WHERE id = %s", (body.status, body.cost_usd, body.error, run_id))
        if body.provider and body.cost_usd:
            conn.execute("INSERT INTO spend_ledger (day, provider, usd) VALUES (current_date, %s, %s) "
                         "ON CONFLICT (day, provider) DO UPDATE SET usd = spend_ledger.usd + EXCLUDED.usd",
                         (body.provider, body.cost_usd))
    return {"ok": True}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    """Public: the demo site polls this to draw the live timeline."""
    with pool().connection() as conn:
        run = conn.execute("SELECT build, status, started_at, finished_at, error FROM runs WHERE id = %s",
                           (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "No such run")
        steps = conn.execute("SELECT name, status, at, duration_ms, detail FROM run_steps WHERE run_id = %s ORDER BY id",
                             (run_id,)).fetchall()
    return {"run_id": run_id, "build": run[0], "status": run[1], "started_at": run[2], "finished_at": run[3],
            "error": run[4], "steps": [{"name": s[0], "status": s[1], "at": s[2], "duration_ms": s[3], "detail": s[4]}
                                       for s in steps]}


@router.get("/stats")
def stats():
    """Public status page: last 30 days per build."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT build, count(*), count(*) FILTER (WHERE status = 'succeeded'), "
            "count(*) FILTER (WHERE status = 'held'), count(*) FILTER (WHERE status = 'failed'), "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) "
            "FROM runs WHERE started_at > now() - interval '30 days' GROUP BY build").fetchall()
    return [{"build": r[0], "runs": r[1], "succeeded": r[2], "held": r[3], "failed": r[4],
             "median_seconds": round(r[5], 1) if r[5] is not None else None} for r in rows]


@router.post("/admin/demos", dependencies=[Depends(require_key)])
def set_demos(paused: bool):
    with pool().connection() as conn:
        conn.execute("UPDATE settings SET value = %s WHERE key = 'demos_paused'", (Jsonb(paused),))
    return {"demos_paused": paused}
