"""Exceptions endpoints. n8n starts the agent, posts the proposal to an approver, and
applies the approver's decision here."""
from __future__ import annotations

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from ..closedesk import repo
from ..closedesk.api import labels
from ..closedesk.extract import to_invoice
from ..closedesk.rules import run_checks
from ..db import pool
from ..platform.api import require_key
from .agent import Agent
from .tools import TAX_RATE, TWO, Case

router = APIRouter(prefix="/v1/exceptions", tags=["exceptions"])

DEMO_CASES = [   # order in labels.json, what makes it interesting
    (41, "Bank change, backed by an email from a look-alike domain."),
    (42, "Bank change from the vendor's real address, with instructions aimed at AI."),
    (43, "Bank change with nothing on file."),
    (45, "Duplicate. The vendor says the original was never paid."),
    (46, "Duplicate worth GHS 118,440."),
    (47, "Price above the PO. Vendor sent a price notice."),
    (48, "Price above the PO. Procurement approved the new price."),
    (49, "Billed for more than was delivered."),
    (50, "Extra delivery the warehouse confirms but nobody ordered."),
]


@router.get("/cases")
def cases():
    """Public: the exception cases the demo page offers."""
    by = {l["order"]: l for l in labels()}
    return [{"case": o, "kind": by[o]["case"], "note": n, "vendor": by[o]["truth"]["vendor_name"],
             "number": by[o]["truth"]["invoice_number"], "total": by[o]["truth"]["total"]}
            for o, n in DEMO_CASES if o in by]


def create_demo_invoice(conn, order: int, run_id: str | None, actor: str = "exceptions-demo") -> str:
    """Puts a fresh copy of a planted invoice on the books (held or pending) for one visitor."""
    label = next((l for l in labels() if l["order"] == order), None)
    if not label or label["case"] == "clean":
        raise HTTPException(404, "No such exception case")
    inv = to_invoice(dict(label["truth"], confidence={}))
    conn.execute("SELECT pg_advisory_lock(42)")
    try:
        report = run_checks(inv, repo.load_master(conn, demo=True))
        return repo.save_invoice(conn, inv, report, None, run_id, actor=actor)
    finally:
        conn.execute("SELECT pg_advisory_unlock(42)")


class WorkBody(BaseModel):
    case: int | None = None           # demo: order of a planted invoice
    invoice_id: str | None = None     # production: a held or pending invoice
    run_id: str | None = None


@router.post("/work", dependencies=[Depends(require_key)])
def work(body: WorkBody):
    """Runs the agent on one invoice and stores its proposal for approval."""
    try:
        agent = Agent()
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    demo = body.case is not None
    with pool().connection() as conn:
        try:
            inv_id = create_demo_invoice(conn, body.case, body.run_id) if demo else body.invoice_id
            if not inv_id:
                raise HTTPException(400, "Send a case or an invoice_id")
            case = Case(conn, inv_id, demo=demo)
            if case.status not in ("held", "pending_approval"):
                raise HTTPException(409, f"Invoice is {case.status}, nothing to resolve")
            res = agent.work(case)
        except HTTPException:
            if body.run_id:
                _finish(conn, body.run_id, "failed", 0, "Could not start the case")
            raise
        except Exception as e:
            if body.run_id:
                _finish(conn, body.run_id, "failed", 0, f"{type(e).__name__}: {e}")
            raise HTTPException(502, f"Agent failed: {type(e).__name__}: {e}")
        pid = save_proposal(conn, case, res, body.run_id, demo)
        if body.run_id:
            for s in res.steps:
                conn.execute("INSERT INTO run_steps (run_id, name, status, duration_ms, detail) VALUES (%s,%s,%s,%s,%s)",
                             (body.run_id, s["name"], s["status"], s.get("ms"), Jsonb(s["detail"])))
            conn.execute("INSERT INTO run_steps (run_id, name, status, detail) VALUES (%s,'Result','ok',%s)",
                         (body.run_id, Jsonb({"proposal_id": pid, "cost_usd": round(res.cost_usd, 5),
                                              "model": res.model, "tool_calls": res.tool_calls})))
            _finish(conn, body.run_id, "held", res.cost_usd, None)    # held = waiting for a person
    return {"proposal_id": pid, "invoice_id": inv_id, "action": res.proposal.get("action"),
            "overridden": res.overridden, "cost_usd": round(res.cost_usd, 5)}


def save_proposal(conn, case: Case, res, run_id, demo) -> str:
    p = res.proposal
    email = p.get("vendor_email")
    if email and case.vendor_email:
        email = {"to": case.vendor_email, "subject": email.get("subject", ""), "body": email.get("body", "")}
    else:
        email = None
    payable = res.payable["payable_total"] if p.get("action") == "approve_adjusted" else (
        float(case.total) if p.get("action") == "approve" else None)
    with conn.transaction():
        pid = conn.execute(
            "INSERT INTO exception_proposals (invoice_id, run_id, is_demo, action, payable_total, summary, evidence, "
            "vendor_email, internal_note, policy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (case.invoice_id, run_id, demo, p["action"], payable, p.get("summary", ""), Jsonb(p.get("evidence", [])),
             Jsonb(email) if email else None, Jsonb(p.get("internal_note")) if p.get("internal_note") else None,
             Jsonb({"violations": res.violations, "overridden": res.overridden, "payable": res.payable,
                    "tool_calls": res.tool_calls}))).fetchone()[0]
        repo.audit(conn, "invoice", case.invoice_id, f"agent:proposed:{p['action']}", "exceptions-agent",
                   {"proposal_id": str(pid)})
    return str(pid)


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    """Public for demo proposals: the proposal, the invoice's status now, emails queued and ledger lines."""
    with pool().connection() as conn:
        r = conn.execute(
            "SELECT p.invoice_id, p.action, p.payable_total, p.summary, p.evidence, p.vendor_email, p.internal_note, "
            "p.policy, p.status, p.decided_by, p.decided_at, p.is_demo, i.status, i.total, i.vendor_name_raw, "
            "i.invoice_number FROM exception_proposals p JOIN invoices i ON i.id = p.invoice_id WHERE p.id = %s",
            (proposal_id,)).fetchone()
        if not r or not r[11]:
            raise HTTPException(404, "No such demo proposal")
        outbox = [dict(to=o[0], subject=o[1], status=o[2]) for o in conn.execute(
            "SELECT to_addr, subject, status FROM outbox WHERE proposal_id = %s", (proposal_id,))]
        ledger = [dict(account=e[0], debit=float(e[1]), credit=float(e[2])) for e in conn.execute(
            "SELECT account, debit, credit FROM ledger_entries WHERE invoice_id = %s ORDER BY id", (r[0],))]
    return {"proposal_id": proposal_id, "invoice_id": str(r[0]), "action": r[1],
            "payable_total": float(r[2]) if r[2] is not None else None, "summary": r[3], "evidence": r[4],
            "vendor_email": r[5], "internal_note": r[6], "policy": {k: r[7].get(k) for k in ("violations", "overridden")},
            "payable": r[7].get("payable"), "status": r[8], "decided_by": r[9], "decided_at": r[10],
            "invoice_status": r[12], "billed_total": float(r[13]), "vendor": r[14], "invoice_number": r[15],
            "outbox": outbox, "ledger": ledger}


class DecisionBody(BaseModel):
    decision: str            # approve | reject
    by: str
    demo_only: bool = False  # the public demo route sets this so it can't touch real proposals


@router.post("/proposals/{proposal_id}/decision", dependencies=[Depends(require_key)])
def decide(proposal_id: str, body: DecisionBody):
    """A person approves or rejects the agent's proposal. Approving is what actually changes the books."""
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    with pool().connection() as conn:
        with conn.transaction():
            r = conn.execute("SELECT invoice_id, action, payable_total, vendor_email, status, is_demo, summary "
                             "FROM exception_proposals WHERE id = %s FOR UPDATE", (proposal_id,)).fetchone()
            if not r or (body.demo_only and not r[5]):
                raise HTTPException(404, "No such proposal")
            inv_id, action, payable, email, status, is_demo, summary = r
            if status != "pending":
                raise HTTPException(409, f"Proposal already {status}")
            conn.execute("UPDATE exception_proposals SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s",
                         ("approved" if body.decision == "approve" else "rejected", body.by, proposal_id))
            result = "Proposal rejected. The invoice stays where it was for a person to handle."
            if body.decision == "approve":
                result = _apply(conn, str(inv_id), action, payable, body.by, summary)
                if email:
                    conn.execute("INSERT INTO outbox (proposal_id, to_addr, subject, body, status) VALUES (%s,%s,%s,%s,%s)",
                                 (proposal_id, email["to"], email["subject"], email["body"],
                                  "demo_not_sent" if is_demo else "queued"))
            repo.audit(conn, "invoice", str(inv_id), f"proposal:{body.decision}", body.by, {"proposal_id": proposal_id})
    out = get_proposal(proposal_id) if is_demo else {"proposal_id": proposal_id}
    out["result"] = result
    return out


def _apply(conn, inv_id: str, action: str, payable, by: str, summary: str) -> str:
    reason = f"Exception agent proposal approved by {by}: {summary[:300]}"
    if action == "approve":
        conn.execute("UPDATE approvals SET decided_at = now(), decision = 'approve', decided_by = %s, reason = %s "
                     "WHERE invoice_id = %s AND decided_at IS NULL", (by, reason, inv_id))
        conn.execute("UPDATE invoices SET status = 'posted' WHERE id = %s", (inv_id,))
        _post(conn, inv_id, None)
        return "Posted to the ledger as billed."
    if action == "approve_adjusted":
        conn.execute("UPDATE approvals SET decided_at = now(), decision = 'approve', decided_by = %s, reason = %s "
                     "WHERE invoice_id = %s AND decided_at IS NULL", (by, reason, inv_id))
        conn.execute("UPDATE invoices SET status = 'posted' WHERE id = %s", (inv_id,))
        _post(conn, inv_id, Decimal(str(payable)))
        return f"Posted GHS {Decimal(str(payable)):,.2f} to the ledger. The rest waits for the vendor's credit note."
    if action == "reject_duplicate":
        conn.execute("UPDATE approvals SET decided_at = now(), decision = 'reject', decided_by = %s, reason = %s "
                     "WHERE invoice_id = %s AND decided_at IS NULL", (by, reason, inv_id))
        conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = %s", (inv_id,))
        return "Rejected as a duplicate. Nothing was posted."
    if action == "escalate_fraud":
        repo.audit(conn, "invoice", inv_id, "escalated:fraud_review", by, {})
        return "Kept on hold and sent to fraud review: call the vendor on the phone number on file."
    repo.audit(conn, "invoice", inv_id, "waiting:info", by, {})
    return "Kept waiting. The request for the missing document has gone out."


def _post(conn, inv_id: str, payable_total: Decimal | None):
    vendor, number, subtotal, tax = conn.execute(
        "SELECT vendor_name_raw, invoice_number, subtotal, tax FROM invoices WHERE id = %s", (inv_id,)).fetchone()
    if payable_total is not None:
        subtotal = (payable_total / (1 + TAX_RATE)).quantize(TWO)
        tax = payable_total - subtotal
    memo = f"{vendor} {number}"
    for acct, dr, cr in (("5000 Expenses", subtotal, 0), ("1400 Input tax", tax, 0),
                         ("2000 Accounts payable", 0, subtotal + tax)):
        conn.execute("INSERT INTO ledger_entries (invoice_id, account, debit, credit, memo) VALUES (%s,%s,%s,%s,%s)",
                     (inv_id, acct, dr, cr, memo))


@router.get("/open", dependencies=[Depends(require_key)])
def open_invoices(limit: int = 10):
    """Production sweep: held or pending invoices with no proposal yet, oldest first."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT i.id FROM invoices i WHERE i.status IN ('held', 'pending_approval') AND NOT EXISTS "
            "(SELECT 1 FROM exception_proposals p WHERE p.invoice_id = i.id) AND i.id::text NOT IN "
            "(SELECT entity_id FROM audit_log WHERE actor IN ('exceptions-demo', 'eval')) "
            "AND i.run_id IS NOT NULL AND i.run_id NOT IN (SELECT id FROM runs WHERE is_demo) "
            "ORDER BY i.created_at LIMIT %s", (min(limit, 25),)).fetchall()
    return [{"invoice_id": str(r[0])} for r in rows]


# ---------- eval on the live system ----------

@router.get("/eval")
def eval_results():
    with pool().connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'exceptions_eval'").fetchone()
    return row[0] if row else {"summary": None}


@router.post("/eval/run", dependencies=[Depends(require_key)])
def eval_run():
    """Runs every planted exception through the agent in the background (about $0.05)."""
    import threading
    threading.Thread(target=_run_eval, daemon=True).start()
    return {"started": True}


def _run_eval():
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
    from exceptions_eval import evaluate
    with pool().connection() as conn:
        _save(conn, {"summary": {"running": True}})
        try:
            result = evaluate(conn, Agent())
            result["summary"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            result = {"summary": {"error": f"{type(e).__name__}: {e}"}}
        _save(conn, result)
        cost = (result.get("summary") or {}).get("cost_usd")
        if cost:
            conn.execute("INSERT INTO spend_ledger (day, provider, usd) VALUES (current_date, 'anthropic', %s) "
                         "ON CONFLICT (day, provider) DO UPDATE SET usd = spend_ledger.usd + EXCLUDED.usd", (cost,))


def _save(conn, value):
    conn.execute("INSERT INTO settings (key, value) VALUES ('exceptions_eval', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (Jsonb(json.loads(json.dumps(value, default=str))),))


def _finish(conn, run_id: str, status: str, cost: float, error: str | None):
    conn.execute("UPDATE runs SET status = %s, finished_at = now(), cost_usd = cost_usd + %s, error = %s WHERE id = %s",
                 (status, cost, error, run_id))
    if cost:
        conn.execute("INSERT INTO spend_ledger (day, provider, usd) VALUES (current_date, 'anthropic', %s) "
                     "ON CONFLICT (day, provider) DO UPDATE SET usd = spend_ledger.usd + EXCLUDED.usd", (cost,))
