"""Database reads and writes for Close Desk."""
from __future__ import annotations

import json
from decimal import Decimal

from psycopg.types.json import Jsonb

from .rules import MasterData, POLine, PriorInvoice, PurchaseOrder, Vendor, norm_ref
from .schemas import CheckReport, ExtractedInvoice


def load_master(conn, demo: bool = False) -> MasterData:
    """demo=True compares against seeded history only, so one visitor's run never
    turns the next visitor's clean invoice into a duplicate."""
    vendors = [Vendor(id=r[0], name=r[1], bank_account=r[2], bank_name=r[3])
               for r in conn.execute("SELECT id, name, bank_account, bank_name FROM vendors")]
    pos: dict[str, PurchaseOrder] = {}
    for pid, num, vid, cur in conn.execute(
            "SELECT id, po_number, vendor_id, currency FROM purchase_orders WHERE status = 'open'"):
        lines = {sku.upper(): POLine(sku=sku.upper(), qty=qty, unit_price=price) for sku, qty, price in conn.execute(
            "SELECT sku, qty, unit_price FROM po_lines WHERE po_id = %s", (pid,))}
        received = {sku.upper(): q for sku, q in conn.execute(
            "SELECT l.sku, sum(l.qty_received) FROM dn_lines l JOIN delivery_notes d ON d.id = l.dn_id "
            "WHERE d.po_id = %s GROUP BY l.sku", (pid,))}
        pos[norm_ref(num)] = PurchaseOrder(po_number=num, vendor_id=vid, currency=cur, lines=lines, received=received)
    prior = [PriorInvoice(vendor_id=r[0], invoice_number_norm=r[1] or "", invoice_date=r[2], total=r[3],
                          file_sha256=r[4])
             for r in conn.execute("SELECT vendor_id, invoice_number_norm, invoice_date, total, file_sha256 "
                                   "FROM invoices WHERE status <> 'rejected'"
                                   + (" AND id::text IN (SELECT entity_id FROM audit_log WHERE actor = 'seed')" if demo else ""))]
    return MasterData(vendors=vendors, purchase_orders=pos, prior_invoices=prior)


def save_invoice(conn, inv: ExtractedInvoice, report: CheckReport, file_sha256: str | None,
                 run_id: str | None, actor: str = "closedesk") -> str:
    """Writes the invoice, its lines and checks, ledger entries when posted, an approval
    request when needed, and an audit row. One transaction."""
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO invoices (run_id, vendor_id, vendor_name_raw, invoice_number, invoice_number_norm,
                   invoice_date, po_number, currency, subtotal, tax, total, bank_name, bank_account,
                   file_sha256, status, extracted, confidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (run_id, report.vendor_id, inv.vendor_name, inv.invoice_number, norm_ref(inv.invoice_number),
             inv.invoice_date, inv.po_number, inv.currency, inv.subtotal, inv.tax, inv.total, inv.bank_name,
             inv.bank_account, file_sha256, report.decision, Jsonb(json.loads(inv.model_dump_json())),
             Jsonb(inv.confidence))).fetchone()
        inv_id = str(row[0])
        for n, l in enumerate(inv.lines, start=1):
            conn.execute("INSERT INTO invoice_lines VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (inv_id, n, l.sku, l.description, l.qty, l.unit_price, l.amount))
        for c in report.checks:
            conn.execute("INSERT INTO invoice_checks VALUES (%s,%s,%s,%s,%s)",
                         (inv_id, c.name, c.result, c.severity, c.detail))
        if report.decision == "posted":
            post_to_ledger(conn, inv_id, inv)
        else:
            conn.execute("INSERT INTO approvals (invoice_id) VALUES (%s)", (inv_id,))
        audit(conn, "invoice", inv_id, f"created:{report.decision}", actor, {"reasons": report.reasons})
    return inv_id


def post_to_ledger(conn, inv_id: str, inv: ExtractedInvoice):
    memo = f"{inv.vendor_name} {inv.invoice_number}"
    subtotal = inv.subtotal or Decimal("0")
    tax = inv.tax or Decimal("0")
    conn.execute("INSERT INTO ledger_entries (invoice_id, account, debit, memo) VALUES (%s,'5000 Expenses',%s,%s)",
                 (inv_id, subtotal, memo))
    conn.execute("INSERT INTO ledger_entries (invoice_id, account, debit, memo) VALUES (%s,'1400 Input tax',%s,%s)",
                 (inv_id, tax, memo))
    conn.execute("INSERT INTO ledger_entries (invoice_id, account, credit, memo) VALUES (%s,'2000 Accounts payable',%s,%s)",
                 (inv_id, subtotal + tax, memo))


def decide(conn, inv_id: str, decision: str, by: str, reason: str | None) -> str:
    """A human approves or rejects from the Slack or WhatsApp card. Fraud holds need a reason."""
    with conn.transaction():
        status, extracted = conn.execute("SELECT status, extracted FROM invoices WHERE id = %s FOR UPDATE",
                                         (inv_id,)).fetchone()
        if status not in ("pending_approval", "held"):
            raise ValueError(f"Invoice is already {status}")
        if status == "held" and decision == "approve" and not reason:
            raise ValueError("Releasing a fraud hold needs a written reason, such as how the bank change was verified")
        conn.execute("UPDATE approvals SET decided_at = now(), decision = %s, decided_by = %s, reason = %s "
                     "WHERE invoice_id = %s AND decided_at IS NULL", (decision, by, reason, inv_id))
        if decision == "approve":
            conn.execute("UPDATE invoices SET status = 'posted' WHERE id = %s", (inv_id,))
            post_to_ledger(conn, inv_id, ExtractedInvoice.model_validate(extracted))
        else:
            conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = %s", (inv_id,))
        audit(conn, "invoice", inv_id, f"human:{decision}", by, {"reason": reason})
    return "posted" if decision == "approve" else "rejected"


def audit(conn, entity: str, entity_id: str, action: str, actor: str, data: dict):
    conn.execute("INSERT INTO audit_log (entity, entity_id, action, actor, data) VALUES (%s,%s,%s,%s,%s)",
                 (entity, entity_id, action, actor, Jsonb(data)))


HISTORY = (5, 10, 16)   # invoices already on the books before the demo starts (originals of the planted duplicates)


def seed_history(conn, labels: list[dict]):
    """Posts the original invoices that the planted duplicates copy. Runs once."""
    from .extract import to_invoice
    from .rules import run_checks
    if conn.execute("SELECT 1 FROM audit_log WHERE actor = 'seed' LIMIT 1").fetchone():
        return
    for item in labels:
        if item["order"] in HISTORY:
            inv = to_invoice(dict(item["truth"], confidence={}))
            report = run_checks(inv, load_master(conn))
            save_invoice(conn, inv, report, file_sha256=None, run_id=None, actor="seed")
