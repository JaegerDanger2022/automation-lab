"""Deterministic checks for Close Desk.

The model reads the invoice. These rules decide what happens to it. Keeping the
decision in plain code means every hold can be explained line by line, and the
rules can be tested without calling a model.

Decision order:
  1. Any fraud-severity failure  -> held       (nobody can approve it from the card)
  2. Any review failure or a low-confidence field -> pending_approval
  3. Otherwise                    -> posted
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .schemas import FIELDS, Check, CheckReport, ExtractedInvoice

MONEY_TOLERANCE = Decimal("0.02")      # rounding on printed invoices
PRICE_TOLERANCE = Decimal("0.02")      # 2% over the PO price is allowed
CONFIDENCE_FLOOR = 0.85
DUPLICATE_WINDOW_DAYS = 14


# ---------- master data handed in by the caller (DB or test fixture) ----------

@dataclass
class Vendor:
    id: int
    name: str
    bank_account: str
    bank_name: str = ""


@dataclass
class POLine:
    sku: str
    qty: Decimal
    unit_price: Decimal


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: int
    currency: str
    lines: dict[str, POLine]                     # by sku
    received: dict[str, Decimal] = field(default_factory=dict)   # sku -> qty on delivery notes


@dataclass
class PriorInvoice:
    vendor_id: int
    invoice_number_norm: str
    invoice_date: date | None
    total: Decimal | None
    file_sha256: str | None


@dataclass
class MasterData:
    vendors: list[Vendor]
    purchase_orders: dict[str, PurchaseOrder]    # by normalised PO number
    prior_invoices: list[PriorInvoice]


# ---------- normalisers ----------

def norm_ref(value: str | None) -> str:
    """'INV-0042', 'inv 42' and 'INV/042' all become 'INV42'."""
    if not value:
        return ""
    raw = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    letters = re.match(r"^[A-Z]*", raw).group(0)
    digits = raw[len(letters):].lstrip("0") or "0"
    return letters + digits


def norm_account(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def norm_name(value: str | None) -> str:
    v = (value or "").lower()
    v = re.sub(r"\b(ltd|limited|co|company|enterprise|enterprises|ventures|gh|ghana)\b", " ", v)
    return re.sub(r"[^a-z0-9]", "", v)


def match_vendor(name: str | None, vendors: list[Vendor]) -> Vendor | None:
    target = norm_name(name)
    if not target:
        return None
    for v in vendors:
        if norm_name(v.name) == target:
            return v
    for v in vendors:                            # forgiving second pass: one contains the other
        n = norm_name(v.name)
        if n and (n in target or target in n):
            return v
    return None


# ---------- the checks ----------

def run_checks(inv: ExtractedInvoice, md: MasterData, file_sha256: str | None = None) -> CheckReport:
    checks: list[Check] = []
    add = checks.append

    vendor = match_vendor(inv.vendor_name, md.vendors)
    if vendor:
        add(Check(name="vendor_known", result="pass", severity="info", detail=vendor.name))
    else:
        add(Check(name="vendor_known", result="fail", severity="review",
                  detail=f"No vendor on file matches '{inv.vendor_name}'"))

    # Bank details: a changed account is the classic invoice fraud.
    if vendor:
        on_invoice = norm_account(inv.bank_account)
        on_file = norm_account(vendor.bank_account)
        if not on_invoice:
            add(Check(name="bank_details", result="warn", severity="review",
                      detail="No bank account printed on the invoice"))
        elif on_invoice != on_file:
            add(Check(name="bank_details", result="fail", severity="fraud",
                      detail=f"Account on invoice ends {on_invoice[-4:]}, account on file ends {on_file[-4:]}"))
        else:
            add(Check(name="bank_details", result="pass", severity="info", detail="Matches account on file"))

    # Duplicates: same number after normalising, same file, or same amount days apart.
    inv_norm = norm_ref(inv.invoice_number)
    dup_detail = None
    for p in md.prior_invoices:
        if file_sha256 and p.file_sha256 == file_sha256:
            dup_detail = "Same file was already processed"
            break
        if vendor and p.vendor_id == vendor.id:
            if inv_norm and p.invoice_number_norm == inv_norm:
                dup_detail = f"Invoice number {inv.invoice_number} already processed for this vendor"
                break
            if (inv.total is not None and p.total == inv.total and inv.invoice_date and p.invoice_date
                    and abs((inv.invoice_date - p.invoice_date).days) <= DUPLICATE_WINDOW_DAYS):
                dup_detail = f"Same total {inv.total} from this vendor within {DUPLICATE_WINDOW_DAYS} days"
                break
    if dup_detail:
        add(Check(name="duplicate", result="fail", severity="fraud", detail=dup_detail))
    else:
        add(Check(name="duplicate", result="pass", severity="info"))

    # Arithmetic.
    problems = []
    for i, line in enumerate(inv.lines, start=1):
        if line.qty is not None and line.unit_price is not None and line.amount is not None:
            if abs(line.qty * line.unit_price - line.amount) > MONEY_TOLERANCE:
                problems.append(f"line {i}: {line.qty} x {line.unit_price} is not {line.amount}")
    line_sum = sum((l.amount for l in inv.lines if l.amount is not None), Decimal("0"))
    if inv.subtotal is not None and inv.lines and abs(line_sum - inv.subtotal) > MONEY_TOLERANCE:
        problems.append(f"lines add to {line_sum}, subtotal says {inv.subtotal}")
    if None not in (inv.subtotal, inv.tax, inv.total) and abs(inv.subtotal + inv.tax - inv.total) > MONEY_TOLERANCE:
        problems.append(f"subtotal {inv.subtotal} + tax {inv.tax} is not total {inv.total}")
    add(Check(name="arithmetic", result="fail" if problems else "pass",
              severity="review" if problems else "info", detail="; ".join(problems)))

    # Three-way match against the PO and what was actually received.
    po = md.purchase_orders.get(norm_ref(inv.po_number))
    if not po:
        add(Check(name="po_match", result="fail", severity="review",
                  detail=f"No open purchase order {inv.po_number or '(none on invoice)'}"))
    else:
        if vendor and po.vendor_id != vendor.id:
            add(Check(name="po_match", result="fail", severity="fraud",
                      detail=f"PO {po.po_number} belongs to a different vendor"))
        else:
            add(Check(name="po_match", result="pass", severity="info", detail=po.po_number))

        price_issues, qty_issues = [], []
        for line in inv.lines:
            pol = po.lines.get((line.sku or "").upper())
            if not pol:
                price_issues.append(f"{line.sku or line.description}: not on the PO")
                continue
            if line.unit_price is not None and line.unit_price > pol.unit_price * (1 + PRICE_TOLERANCE):
                price_issues.append(f"{line.sku}: billed {line.unit_price}, PO price {pol.unit_price}")
            received = po.received.get(pol.sku, Decimal("0"))
            if line.qty is not None and line.qty > received:
                qty_issues.append(f"{line.sku}: billed {line.qty}, received {received}")
        add(Check(name="price_vs_po", result="fail" if price_issues else "pass",
                  severity="review" if price_issues else "info", detail="; ".join(price_issues)))
        add(Check(name="qty_vs_received", result="fail" if qty_issues else "pass",
                  severity="review" if qty_issues else "info", detail="; ".join(qty_issues)))

    # Confidence from the extractor.
    low = [f for f in FIELDS if inv.confidence.get(f, 1.0) < CONFIDENCE_FLOOR]
    add(Check(name="extraction_confidence", result="warn" if low else "pass",
              severity="review" if low else "info",
              detail=("Low confidence: " + ", ".join(low)) if low else ""))

    fraud = [c for c in checks if c.severity == "fraud" and c.result == "fail"]
    review = [c for c in checks if c.severity == "review" and c.result in ("fail", "warn")]
    if fraud:
        decision = "held"
    elif review:
        decision = "pending_approval"
    else:
        decision = "posted"
    reasons = [f"{c.name}: {c.detail}" for c in fraud + review]
    return CheckReport(decision=decision, checks=checks, reasons=reasons,
                       vendor_id=vendor.id if vendor else None)
