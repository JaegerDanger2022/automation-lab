from decimal import Decimal as D
from datetime import date
from lab.closedesk.rules import MasterData, POLine, PriorInvoice, PurchaseOrder, Vendor, norm_ref, run_checks
from lab.closedesk.schemas import ExtractedInvoice, InvoiceLine

V = Vendor(id=1, name="Adom Office Supplies Ltd", bank_account="1234567890123")
PO = PurchaseOrder(po_number="PO-2026-0001", vendor_id=1, currency="GHS",
                   lines={"PAP-A4": POLine("PAP-A4", D("10"), D("285.00"))}, received={"PAP-A4": D("10")})

def md(prior=None):
    return MasterData(vendors=[V], purchase_orders={norm_ref("PO-2026-0001"): PO}, prior_invoices=prior or [])

def inv(**kw):
    base = dict(vendor_name="ADOM OFFICE SUPPLIES", invoice_number="INV-0101", invoice_date=date(2026, 6, 10),
                po_number="PO-2026-0001", currency="GHS", subtotal=D("2850.00"), tax=D("570.00"), total=D("3420.00"),
                bank_account="1234 5678 90123",
                lines=[InvoiceLine(sku="PAP-A4", qty=D("10"), unit_price=D("285.00"), amount=D("2850.00"))])
    base.update(kw)
    return ExtractedInvoice(**base)

def test_clean_invoice_posts():
    assert run_checks(inv(), md()).decision == "posted"

def test_changed_bank_account_is_held():
    r = run_checks(inv(bank_account="9999999999999"), md())
    assert r.decision == "held" and any("bank_details" in x for x in r.reasons)

def test_duplicate_with_different_number_format_is_held():
    prior = [PriorInvoice(vendor_id=1, invoice_number_norm=norm_ref("INV-0101"), invoice_date=date(2026, 6, 10),
                          total=D("3420.00"), file_sha256="abc")]
    assert run_checks(inv(invoice_number="inv/00101"), md(prior)).decision == "held"

def test_price_above_po_needs_approval():
    r = run_checks(inv(lines=[InvoiceLine(sku="PAP-A4", qty=D("10"), unit_price=D("300.00"), amount=D("3000.00"))],
                       subtotal=D("3000.00"), tax=D("600.00"), total=D("3600.00")), md())
    assert r.decision == "pending_approval"

def test_qty_above_received_needs_approval():
    r = run_checks(inv(lines=[InvoiceLine(sku="PAP-A4", qty=D("12"), unit_price=D("285.00"), amount=D("3420.00"))],
                       subtotal=D("3420.00"), tax=D("684.00"), total=D("4104.00")), md())
    assert r.decision == "pending_approval" and any("qty_vs_received" in x for x in r.reasons)

def test_bad_arithmetic_needs_approval():
    assert run_checks(inv(total=D("3500.00")), md()).decision == "pending_approval"

def test_low_confidence_needs_approval():
    assert run_checks(inv(confidence={"total": 0.4}), md()).decision == "pending_approval"

def test_norm_ref():
    assert norm_ref("INV-0042") == norm_ref("inv 42") == norm_ref("INV/042") == "INV42"
