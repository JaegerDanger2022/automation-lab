"""Data shapes for Close Desk. The extractor fills ExtractedInvoice, the rules read it."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

FIELDS = [
    "vendor_name", "invoice_number", "invoice_date", "po_number", "currency",
    "subtotal", "tax", "total", "bank_name", "bank_account",
]


class InvoiceLine(BaseModel):
    sku: str | None = None
    description: str | None = None
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class ExtractedInvoice(BaseModel):
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    po_number: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    # 0 to 1 per field, from the extractor. Missing key means unknown.
    confidence: dict[str, float] = Field(default_factory=dict)


class Check(BaseModel):
    name: str
    result: Literal["pass", "warn", "fail"]
    severity: Literal["info", "review", "fraud"]
    detail: str = ""


Decision = Literal["posted", "pending_approval", "held"]


class CheckReport(BaseModel):
    decision: Decision
    checks: list[Check]
    reasons: list[str]
    vendor_id: int | None = None
