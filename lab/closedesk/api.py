"""Close Desk endpoints. n8n orchestrates; these do the reading, checking and booking."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..db import pool
from ..platform.api import require_key
from . import repo
from .extract import extract
from .rules import run_checks
from .schemas import ExtractedInvoice

router = APIRouter(prefix="/v1/closedesk", tags=["closedesk"])
SAMPLES = Path(__file__).resolve().parents[2] / "data" / "closedesk" / "invoices"
ALLOWED = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 8 * 1024 * 1024

# Rough per-million-token prices for the cost log. Set to the model you deploy with.
PRICE_IN, PRICE_OUT = 3.0, 15.0


@router.get("/samples")
def samples():
    """Public: the sample invoices the demo page offers."""
    labels = json.loads((SAMPLES.parent / "labels.json").read_text())
    picks = [l for l in labels if l["case"] != "clean"][:6] + [l for l in labels if l["case"] == "clean"][:6]
    return [{"file": l["file"], "case": l["case"]} for l in picks]


@router.get("/samples/{name}")
def sample_file(name: str):
    path = (SAMPLES / name).resolve()
    if path.parent != SAMPLES.resolve() or not path.exists():
        raise HTTPException(404, "No such sample")
    return FileResponse(path)


@router.post("/extract", dependencies=[Depends(require_key)])
async def extract_endpoint(file: UploadFile | None = File(default=None), sample: str | None = Form(default=None)):
    """Reads one invoice. Send a file upload or the name of a sample."""
    if sample:
        path = (SAMPLES / sample).resolve()
        if path.parent != SAMPLES.resolve() or not path.exists():
            raise HTTPException(404, "No such sample")
        data = path.read_bytes()
        media = "application/pdf" if path.suffix == ".pdf" else "image/jpeg"
    elif file:
        data = await file.read()
        media = file.content_type or ""
    else:
        raise HTTPException(400, "Send a file or a sample name")
    if media not in ALLOWED:
        raise HTTPException(415, f"Unsupported type {media}")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File over 8 MB")
    try:
        inv, usage = extract(data, media)
    except RuntimeError as e:                        # model not configured
        raise HTTPException(503, str(e))
    except Exception as e:                           # provider error: n8n retries, then alerts
        raise HTTPException(502, f"Extraction failed: {type(e).__name__}: {e}")
    cost = (usage["input_tokens"] * PRICE_IN + usage["output_tokens"] * PRICE_OUT) / 1_000_000
    return {"invoice": json.loads(inv.model_dump_json()), "file_sha256": hashlib.sha256(data).hexdigest(),
            "usage": usage, "cost_usd": round(cost, 5)}


class CheckBody(BaseModel):
    invoice: ExtractedInvoice
    file_sha256: str | None = None


@router.post("/check", dependencies=[Depends(require_key)])
def check(body: CheckBody):
    """Runs every rule and returns the decision with reasons. Writes nothing."""
    with pool().connection() as conn:
        report = run_checks(body.invoice, repo.load_master(conn), file_sha256=body.file_sha256)
    return json.loads(report.model_dump_json())


class RecordBody(BaseModel):
    invoice: ExtractedInvoice
    file_sha256: str | None = None
    run_id: str | None = None


@router.post("/record", dependencies=[Depends(require_key)])
def record(body: RecordBody):
    """Checks again inside the write (so two parallel runs can't both post) and saves everything."""
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_lock(42)")
        try:
            report = run_checks(body.invoice, repo.load_master(conn), file_sha256=body.file_sha256)
            inv_id = repo.save_invoice(conn, body.invoice, report, body.file_sha256, body.run_id)
        finally:
            conn.execute("SELECT pg_advisory_unlock(42)")
    return {"invoice_id": inv_id, "decision": report.decision, "reasons": report.reasons,
            "checks": json.loads(report.model_dump_json())["checks"]}


class DecisionBody(BaseModel):
    decision: str                     # approve | reject
    by: str
    reason: str | None = None


@router.post("/invoices/{invoice_id}/decision", dependencies=[Depends(require_key)])
def decide(invoice_id: str, body: DecisionBody):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    with pool().connection() as conn:
        try:
            status = repo.decide(conn, invoice_id, body.decision, body.by, body.reason)
        except ValueError as e:
            raise HTTPException(409, str(e))
    return {"invoice_id": invoice_id, "status": status}


@router.get("/ledger")
def ledger(limit: int = 20):
    """Public: the demo ledger, newest first."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT i.id, i.vendor_name_raw, i.invoice_number, i.total, i.status, i.created_at, "
            "coalesce(array_agg(c.check_name || ': ' || c.detail) FILTER (WHERE c.result <> 'pass'), '{}') "
            "FROM invoices i LEFT JOIN invoice_checks c ON c.invoice_id = i.id "
            "GROUP BY i.id ORDER BY i.created_at DESC LIMIT %s", (min(limit, 100),)).fetchall()
    return [{"invoice_id": str(r[0]), "vendor": r[1], "number": r[2], "total": r[3], "status": r[4],
             "at": r[5], "issues": r[6]} for r in rows]
