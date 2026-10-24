"""Hard limits on what the exceptions agent may propose. Checked in code after every proposal.

The model chooses an action and writes the explanation. These rules decide whether
the proposal is allowed to reach an approver:

  1. A bank account change is only ever escalated for a phone call to the number on
     file. No email goes out, because the email channel may be the compromised one.
  2. Any other fraud check (duplicate, PO belongs to another vendor) can only be
     rejected as a duplicate or escalated.
  3. Paying more than calculate_payable needs a Northwind document (source internal)
     the agent actually read: a price_amendment covering that SKU dated on or before
     the invoice, or a receipt_confirmation, and never past the PO quantity.
  4. approve_adjusted must use the amount the code calculated, to the cent.
  5. Evidence may only cite documents the agent opened.
  6. Emails go to the vendor's address on file, never contain a full account number,
     and stay short.
"""
from __future__ import annotations

import re
from decimal import Decimal

FRAUD_ONLY = {"escalate_fraud"}
DUPLICATE_OK = {"reject_duplicate", "escalate_fraud"}


def check(p: dict, case, payable: dict) -> list[str]:
    errors: list[str] = []
    action = p.get("action")
    failed = {c["check"]: c for c in case.failed if c["result"] in ("fail", "warn")}
    fraud = {n for n, c in failed.items() if c["severity"] == "fraud"}

    if "bank_details" in fraud:
        if action not in FRAUD_ONLY:
            errors.append("The bank account differs from the one on file. The only allowed action is "
                          "escalate_fraud, so someone calls the vendor on the phone number on file.")
        if p.get("vendor_email"):
            errors.append("Do not email the vendor about a bank change. The email channel may be compromised; "
                          "verification is by phone only.")
    elif fraud:
        if action not in DUPLICATE_OK:
            errors.append(f"Fraud check failed ({', '.join(sorted(fraud))}). Allowed actions: reject_duplicate "
                          "or escalate_fraud.")

    cited = " ".join(p.get("evidence", []))
    cited_docs = {int(m) for m in re.findall(r"DOC-(\d+)", cited + " " + p.get("summary", ""))}
    unseen = cited_docs - set(case.seen_docs)
    if unseen:
        errors.append(f"You cited {', '.join(f'DOC-{d}' for d in sorted(unseen))} without opening it.")

    if action == "approve" and not fraud:
        billed = Decimal(str(payable["billed_total"]))
        owed = Decimal(str(payable["payable_total"]))
        if billed - owed > Decimal("0.05"):
            errors += _approve_over_payable(case, payable, cited_docs)

    if action == "approve_adjusted":
        want = Decimal(str(payable["payable_total"]))
        got = p.get("payable_total")
        if got is None or abs(Decimal(str(got)) - want) > Decimal("0.01"):
            errors.append(f"payable_total must be {want:.2f} (from calculate_payable), got {got}.")

    email = p.get("vendor_email") or {}
    body = f"{email.get('subject', '')} {email.get('body', '')}"
    if email and re.search(r"\d{10,}", body.replace(" ", "")):
        errors.append("The email contains what looks like a full account number. Never write account numbers.")
    if email and len(body) > 1500:
        errors.append("Keep the email under 1500 characters.")
    return errors


def _approve_over_payable(case, payable: dict, cited: set[int]) -> list[str]:
    errors = []
    docs = [case.seen_docs[i] for i in cited if i in case.seen_docs]
    po_qty = {l["sku"].upper(): l["qty"] for l in case.get_purchase_order().get("lines", [])}
    for l in payable["lines"]:
        sku = l["sku"]
        if l["billed_price"] > l["payable_price"]:
            ok = any(d["doc_type"] == "price_amendment" and d["source"] == "internal" and (d["sku"] or "").upper() == sku
                     and (d["effective_on"] or "9999") <= str(case.date) for d in docs)
            if not ok:
                errors.append(f"{sku} is billed above the PO price. Paying it needs an internal price_amendment "
                              "for this SKU, in effect by the invoice date, cited as evidence. Otherwise use "
                              "approve_adjusted or hold_for_info.")
        if l["billed_qty"] > l["payable_qty"]:
            confirmed = any(d["doc_type"] == "receipt_confirmation" and d["source"] == "internal"
                            and (d["sku"] or "").upper() == sku for d in docs)
            if not confirmed:
                errors.append(f"{sku}: billed {l['billed_qty']} but only {l['payable_qty']} received. Paying for "
                              "goods not received needs an internal receipt_confirmation cited as evidence.")
            elif l["billed_qty"] > po_qty.get(sku, 0):
                errors.append(f"{sku}: billed {l['billed_qty']} but the PO ordered {po_qty.get(sku, 0)}. Goods "
                              "beyond the PO need a PO amendment from procurement first; use hold_for_info or "
                              "approve_adjusted.")
    return errors


def fallback(case, reason: str) -> dict:
    """Used when the agent can't produce an allowed proposal: hand the case to a person."""
    fraud = any(c["severity"] == "fraud" for c in case.failed if c["result"] == "fail")
    return {"action": "escalate_fraud" if fraud else "hold_for_info",
            "summary": f"The agent could not produce a proposal that passes policy ({reason}). "
                       "A person needs to review this invoice.",
            "evidence": [], "internal_note": {"team": "fraud" if fraud else "ap",
                                              "message": "Agent handed this case over for manual review."}}
