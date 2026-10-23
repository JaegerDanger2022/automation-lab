"""Invoice extraction with Claude.

Two paths, chosen per file:
  * PDF with a text layer -> send the text only (cheap, fast)
  * Scan, photo or image-only PDF -> send the page itself (vision)

The model must answer through a tool call, so the output always matches the schema.
It also returns a 0-1 confidence per field, which the rules use to route doubtful
invoices to a human.
"""
from __future__ import annotations

import base64
import io
import os
from decimal import Decimal, InvalidOperation

import pdfplumber

from .schemas import FIELDS, ExtractedInvoice, InvoiceLine

MIN_TEXT_CHARS = 80   # below this a PDF is treated as a scan

TOOL = {
    "name": "record_invoice",
    "description": "Record the fields read from one supplier invoice.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name": {"type": ["string", "null"]},
            "invoice_number": {"type": ["string", "null"]},
            "invoice_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"},
            "po_number": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"], "description": "ISO code, e.g. GHS"},
            "subtotal": {"type": ["number", "null"]},
            "tax": {"type": ["number", "null"], "description": "Total of all taxes and levies"},
            "total": {"type": ["number", "null"]},
            "bank_name": {"type": ["string", "null"]},
            "bank_account": {"type": ["string", "null"], "description": "Account number exactly as printed"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "qty": {"type": ["number", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "amount": {"type": ["number", "null"]},
                    },
                },
            },
            "confidence": {
                "type": "object",
                "description": "0 to 1 for each top-level field. Lower it when the value is blurred, "
                               "handwritten, cut off or guessed.",
                "properties": {f: {"type": "number"} for f in FIELDS},
            },
        },
        "required": ["vendor_name", "invoice_number", "total", "lines", "confidence"],
    },
}

SYSTEM = (
    "You read supplier invoices for an accounts payable team in Ghana. "
    "Copy values exactly as printed. Never invent a value: use null when a field is missing. "
    "Amounts are plain numbers without currency symbols or thousand separators. "
    "Tax means the sum of VAT, NHIL, GETFund and COVID levies when they are listed separately."
)


def pdf_text(data: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def to_invoice(raw: dict) -> ExtractedInvoice:
    lines = [InvoiceLine(sku=l.get("sku"), description=l.get("description"), qty=_dec(l.get("qty")),
                         unit_price=_dec(l.get("unit_price")), amount=_dec(l.get("amount")))
             for l in raw.get("lines") or []]
    return ExtractedInvoice(
        vendor_name=raw.get("vendor_name"), invoice_number=raw.get("invoice_number"),
        invoice_date=raw.get("invoice_date") or None, po_number=raw.get("po_number"),
        currency=raw.get("currency"), subtotal=_dec(raw.get("subtotal")), tax=_dec(raw.get("tax")),
        total=_dec(raw.get("total")), bank_name=raw.get("bank_name"), bank_account=raw.get("bank_account"),
        lines=lines, confidence={k: float(v) for k, v in (raw.get("confidence") or {}).items()},
    )


def build_content(data: bytes, media_type: str) -> tuple[list[dict], str]:
    """Returns the message content and which path was used ('text' or 'vision')."""
    if media_type == "application/pdf":
        text = pdf_text(data)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            return [{"type": "text", "text": f"Invoice text:\n\n{text}"}], "text"
        block = {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode()}}
    else:
        block = {"type": "image",
                 "source": {"type": "base64", "media_type": media_type,
                            "data": base64.b64encode(data).decode()}}
    return [block, {"type": "text", "text": "Read this invoice."}], "vision"


def extract(data: bytes, media_type: str, client=None) -> tuple[ExtractedInvoice, dict]:
    """Returns the invoice and usage info: path, model, input/output tokens."""
    import anthropic

    model = os.environ.get("CLOSEDESK_MODEL")
    if not model:
        raise RuntimeError("Set CLOSEDESK_MODEL to the Claude model id to use")
    client = client or anthropic.Anthropic()
    content, path = build_content(data, media_type)
    msg = client.messages.create(
        model=model, max_tokens=2000, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "record_invoice"},
        messages=[{"role": "user", "content": content}],
    )
    raw = next(b.input for b in msg.content if b.type == "tool_use")
    usage = {"path": path, "model": model,
             "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}
    return to_invoice(raw), usage
