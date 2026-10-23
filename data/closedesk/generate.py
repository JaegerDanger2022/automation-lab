"""Builds the Close Desk test set: vendors, purchase orders, delivery notes and 50 invoices.

Outputs (in this folder):
  seed.sql          master data for the database
  labels.json       ground truth for every invoice, plus the expected decision
  invoices/*.pdf    rendered invoices, three different layouts
  invoices/*.jpg    a subset turned into phone-photo style scans

Planted cases (10): 3 changed bank accounts, 3 resubmitted duplicates,
2 prices above the PO, 2 quantities above what was received.
Everything is fictional. Run: python data/closedesk/generate.py
"""
from __future__ import annotations

import json
import random
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageFilter
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent
INV_DIR = OUT / "invoices"
random.seed(7)
TWO = Decimal("0.01")

BANKS = ["GCB Bank", "Ecobank Ghana", "Fidelity Bank", "Absa Bank Ghana", "Stanbic Bank", "CalBank"]
VENDORS = [
    ("Adom Office Supplies Ltd", [("PAP-A4", "A4 copier paper, box of 5 reams", 285), ("TON-85A", "Toner cartridge 85A", 620), ("FIL-LA", "Lever arch files, pack of 10", 140)]),
    ("Kasoa Building Materials Ltd", [("CEM-50", "Cement 50kg bag", 98), ("ROD-12", "Iron rod 12mm, length", 165), ("BLK-6", "Sandcrete block 6 inch", 9)]),
    ("Volta Clean Water Co", [("WAT-19", "Bottled water 19L", 22), ("DSP-01", "Water dispenser rental, monthly", 150)]),
    ("Nyansa IT Services", [("LAP-14", "Laptop 14 inch, i5, 16GB", 9800), ("MON-24", "Monitor 24 inch", 2100), ("SUP-HR", "On-site IT support, per hour", 350)]),
    ("Tema Fuel & Lubricants", [("DSL-L", "Diesel, per litre", 15), ("OIL-5", "Engine oil 5L", 420)]),
    ("Accra Print House", [("FLY-A5", "A5 flyers, per 1000", 900), ("BAN-RU", "Roll-up banner", 650)]),
    ("Sunyani Agro Inputs Ltd", [("FER-NPK", "NPK fertiliser 50kg", 480), ("SED-MZ", "Maize seed 10kg", 260)]),
    ("Golden Cleaning Services", [("CLN-MO", "Office cleaning, monthly contract", 3200), ("SAN-KIT", "Sanitation kit", 180)]),
    ("Pokuase Hardware Enterprise", [("PNT-20", "Emulsion paint 20L", 760), ("NAL-4", "Nails 4 inch, 1kg", 35), ("PVC-4", "PVC pipe 4 inch", 120)]),
    ("Spintex Electricals Ltd", [("CAB-2.5", "Cable 2.5mm, 100m roll", 1150), ("SWT-2G", "Double switch", 28), ("LED-18", "LED tube 18W", 45)]),
    ("Labadi Catering Ventures", [("LUN-PP", "Staff lunch, per person", 75), ("WAT-SAC", "Sachet water bag", 8)]),
    ("Cape Coast Logistics Ltd", [("TRK-DAY", "Truck hire, per day", 2800), ("LAB-DAY", "Loading crew, per day", 600)]),
]


def acct() -> str:
    return "".join(random.choice("0123456789") for _ in range(13))


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWO)


def build_master():
    vendors = []
    for i, (name, catalog) in enumerate(VENDORS, start=1):
        vendors.append({"id": i, "name": name, "tin": f"C00{random.randint(10000000, 99999999)}",
                        "bank_name": random.choice(BANKS), "bank_account": acct(), "catalog": catalog,
                        "email": f"accounts@{name.split()[0].lower()}.example"})
    pos, dns = [], []
    start = date(2026, 6, 1)
    for n in range(1, 41):
        v = vendors[(n - 1) % len(vendors)]
        items = random.sample(v["catalog"], k=random.randint(1, len(v["catalog"])))
        lines = [{"sku": sku, "description": d, "qty": money(random.choice([1, 2, 3, 5, 10, 20, 40])),
                  "unit_price": money(p)} for sku, d, p in items]
        issued = start + timedelta(days=n * 2)
        po = {"po_number": f"PO-2026-{n:04d}", "vendor_id": v["id"], "issued_on": issued, "lines": lines}
        pos.append(po)
        dns.append({"dn_number": f"DN-{n:04d}", "po_number": po["po_number"],
                    "received_on": issued + timedelta(days=5),
                    "lines": [{"sku": l["sku"], "qty_received": l["qty"]} for l in lines]})
    return vendors, pos, dns


def invoice_from_po(po, vendor, number: str, inv_date: date, template: str) -> dict:
    lines = [{"sku": l["sku"], "description": l["description"], "qty": l["qty"],
              "unit_price": l["unit_price"], "amount": (l["qty"] * l["unit_price"]).quantize(TWO)}
             for l in po["lines"]]
    subtotal = sum((l["amount"] for l in lines), Decimal("0"))
    vat = (subtotal * Decimal("0.15")).quantize(TWO)
    nhil = (subtotal * Decimal("0.025")).quantize(TWO)
    getf = (subtotal * Decimal("0.025")).quantize(TWO)
    tax = vat + nhil + getf
    return {"vendor_name": vendor["name"], "invoice_number": number, "invoice_date": inv_date,
            "po_number": po["po_number"], "currency": "GHS", "subtotal": subtotal,
            "tax_parts": {"VAT 15%": vat, "NHIL 2.5%": nhil, "GETFund 2.5%": getf}, "tax": tax,
            "total": subtotal + tax, "bank_name": vendor["bank_name"], "bank_account": vendor["bank_account"],
            "lines": lines, "template": template}


def recompute(inv):
    for l in inv["lines"]:
        l["amount"] = (l["qty"] * l["unit_price"]).quantize(TWO)
    sub = sum((l["amount"] for l in inv["lines"]), Decimal("0"))
    parts = {"VAT 15%": Decimal("0.15"), "NHIL 2.5%": Decimal("0.025"), "GETFund 2.5%": Decimal("0.025")}
    inv["tax_parts"] = {k: (sub * r).quantize(TWO) for k, r in parts.items()}
    inv["subtotal"], inv["tax"] = sub, sum(inv["tax_parts"].values(), Decimal("0"))
    inv["total"] = sub + inv["tax"]


def fmt(x: Decimal, style: str) -> str:
    s = f"{x:,.2f}"
    return {"plain": f"{x:.2f}", "comma": s, "cedi": f"GH₵ {s}"}[style]


def render_pdf(inv: dict, path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    t = inv["template"]
    style = {"A": "plain", "B": "comma", "C": "cedi"}[t]
    y = h - 25 * mm
    if t == "A":
        c.setFont("Helvetica-Bold", 16); c.drawString(20 * mm, y, inv["vendor_name"])
        c.setFont("Helvetica", 10); c.drawString(20 * mm, y - 6 * mm, "Accra, Ghana")
        c.setFont("Helvetica-Bold", 20); c.drawRightString(w - 20 * mm, y, "INVOICE")
        c.setFont("Helvetica", 10)
        meta = [("Invoice No:", inv["invoice_number"]), ("Date:", inv["invoice_date"].strftime("%d/%m/%Y")),
                ("PO Ref:", inv["po_number"])]
    elif t == "B":
        c.setFont("Helvetica-Bold", 14); c.drawCentredString(w / 2, y, inv["vendor_name"].upper())
        c.setFont("Helvetica", 9); c.drawCentredString(w / 2, y - 5 * mm, "Suppliers of quality goods and services")
        c.setFont("Helvetica-Bold", 12); c.drawString(20 * mm, y - 15 * mm, "BILL")
        c.setFont("Helvetica", 10)
        meta = [("Bill No.", inv["invoice_number"]), ("Issued", inv["invoice_date"].strftime("%d %b %Y")),
                ("Your Order", inv["po_number"])]
        y -= 10 * mm
    else:
        c.setFont("Helvetica-Bold", 18); c.drawString(20 * mm, y, "TAX INVOICE")
        c.setFont("Helvetica", 10); c.drawRightString(w - 20 * mm, y, inv["vendor_name"])
        meta = [("Ref", inv["invoice_number"]), ("Invoice date", inv["invoice_date"].isoformat()),
                ("Purchase order", inv["po_number"])]
    yy = y - 16 * mm
    for k, v in meta:
        c.drawString(130 * mm, yy, k); c.drawString(160 * mm, yy, v); yy -= 5 * mm
    c.drawString(20 * mm, y - 16 * mm, "Bill to: Northwind Ghana Ltd, Airport City, Accra")
    yy = y - 40 * mm
    c.setFont("Helvetica-Bold", 10)
    cols = [20, 45, 120, 140, 165]
    for x, hd in zip(cols, ["Item", "Description", "Qty", "Unit price", "Amount"]):
        c.drawString(x * mm, yy, hd)
    c.line(20 * mm, yy - 2 * mm, w - 20 * mm, yy - 2 * mm)
    c.setFont("Helvetica", 10)
    for l in inv["lines"]:
        yy -= 7 * mm
        c.drawString(20 * mm, yy, l["sku"]); c.drawString(45 * mm, yy, l["description"][:38])
        c.drawString(120 * mm, yy, f"{l['qty']:g}" if l["qty"] == l["qty"].to_integral() else str(l["qty"]))
        c.drawString(140 * mm, yy, fmt(l["unit_price"], style)); c.drawString(165 * mm, yy, fmt(l["amount"], style))
    yy -= 12 * mm
    rows = [("Subtotal", inv["subtotal"])] + list(inv["tax_parts"].items()) + [("TOTAL DUE", inv["total"])]
    for k, v in rows:
        c.setFont("Helvetica-Bold" if k == "TOTAL DUE" else "Helvetica", 10)
        c.drawString(130 * mm, yy, k); c.drawRightString(w - 20 * mm, yy, fmt(v, style)); yy -= 6 * mm
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, 40 * mm, "Payment details")
    c.drawString(20 * mm, 35 * mm, f"Bank: {inv['bank_name']}")
    c.drawString(20 * mm, 30 * mm, f"Account number: {inv['bank_account']}")
    c.drawString(20 * mm, 25 * mm, f"Account name: {inv['vendor_name']}")
    c.save()


def to_scan(pdf: Path, jpg: Path):
    tmp = jpg.with_name(jpg.stem + "_raw")
    subprocess.run(["pdftoppm", "-jpeg", "-r", "110", "-singlefile", str(pdf), str(tmp)], check=True)
    img = Image.open(str(tmp) + ".jpg").convert("L")
    img = img.rotate(random.uniform(-2.5, 2.5), expand=True, fillcolor=235)
    arr = np.asarray(img).astype(np.int16)
    arr = arr - 12 + np.random.default_rng(len(pdf.name)).normal(0, 9, arr.shape).astype(np.int16)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    img.save(jpg, quality=70)
    Path(str(tmp) + ".jpg").unlink(missing_ok=True)


def main():
    INV_DIR.mkdir(exist_ok=True)
    vendors, pos, dns = build_master()
    vmap = {v["id"]: v for v in vendors}
    invoices = []
    seq = 100
    for i, po in enumerate(pos):                       # 40 clean invoices, one per PO
        v = vmap[po["vendor_id"]]
        seq += 1
        inv = invoice_from_po(po, v, f"INV-{seq:04d}", po["issued_on"] + timedelta(days=7), "ABC"[i % 3])
        inv.update(case="clean", expected="posted")
        invoices.append(inv)

    # Planted: changed bank details on three new invoices.
    for k, po in enumerate(pos[:3]):
        v = vmap[po["vendor_id"]]
        seq += 1
        inv = invoice_from_po(po, v, f"INV-{seq:04d}", po["issued_on"] + timedelta(days=40), "ABC"[k])
        inv["bank_account"], inv["bank_name"] = acct(), random.choice(BANKS)
        inv.update(case="bank_changed", expected="held")
        invoices.append(inv)
    # Planted: duplicates resubmitted with the number written differently.
    for k, src in enumerate([invoices[4], invoices[9], invoices[15]]):
        dup = json.loads(json.dumps(src, default=str))
        n = int(src["invoice_number"].split("-")[1])
        dup_inv = dict(src, invoice_number=[f"INV {n}", f"inv/{n:05d}", f"INV{n}"][k],
                       invoice_date=src["invoice_date"] + timedelta(days=6), template="CAB"[k],
                       lines=[dict(l) for l in src["lines"]], tax_parts=dict(src["tax_parts"]))
        dup_inv.update(case="duplicate", expected="held")
        invoices.append(dup_inv)
    # Planted: price above PO (two) and quantity above received (two).
    for k, po in enumerate(pos[20:22]):
        v = vmap[po["vendor_id"]]; seq += 1
        inv = invoice_from_po(po, v, f"INV-{seq:04d}", po["issued_on"] + timedelta(days=45), "AB"[k])
        inv["lines"][0]["unit_price"] = (inv["lines"][0]["unit_price"] * Decimal("1.12")).quantize(TWO)
        recompute(inv); inv.update(case="price_above_po", expected="pending_approval"); invoices.append(inv)
    for k, po in enumerate(pos[25:27]):
        v = vmap[po["vendor_id"]]; seq += 1
        inv = invoice_from_po(po, v, f"INV-{seq:04d}", po["issued_on"] + timedelta(days=45), "BC"[k])
        inv["lines"][0]["qty"] = inv["lines"][0]["qty"] + 5
        recompute(inv); inv.update(case="qty_above_received", expected="pending_approval"); invoices.append(inv)

    scan_ids = set(random.sample(range(len(invoices)), 12))
    labels = []
    for idx, inv in enumerate(invoices):
        fid = f"{idx + 1:02d}"
        pdf = INV_DIR / f"invoice_{fid}.pdf"
        render_pdf(inv, pdf)
        fname = pdf.name
        if idx in scan_ids:
            jpg = INV_DIR / f"invoice_{fid}.jpg"
            to_scan(pdf, jpg)
            pdf.unlink()
            fname = jpg.name
        labels.append({
            "file": fname, "order": idx + 1, "case": inv["case"], "expected_decision": inv["expected"],
            "truth": {
                "vendor_name": inv["vendor_name"], "invoice_number": inv["invoice_number"],
                "invoice_date": inv["invoice_date"].isoformat(), "po_number": inv["po_number"],
                "currency": "GHS", "subtotal": str(inv["subtotal"]), "tax": str(inv["tax"]),
                "total": str(inv["total"]), "bank_name": inv["bank_name"], "bank_account": inv["bank_account"],
                "lines": [{k: str(v) for k, v in l.items()} for l in inv["lines"]],
            },
        })
    (OUT / "labels.json").write_text(json.dumps(labels, indent=2))

    sql = ["-- Generated by data/closedesk/generate.py. Fictional data.", "BEGIN;",
           "TRUNCATE dn_lines, delivery_notes, po_lines, purchase_orders, vendor_bank_history, vendors RESTART IDENTITY CASCADE;"]
    q = lambda s: "'" + str(s).replace("'", "''") + "'"
    for v in vendors:
        sql.append(f"INSERT INTO vendors (id, name, tin, email, bank_name, bank_account) VALUES "
                   f"({v['id']}, {q(v['name'])}, {q(v['tin'])}, {q(v['email'])}, {q(v['bank_name'])}, {q(v['bank_account'])});")
    for n, po in enumerate(pos, start=1):
        sql.append(f"INSERT INTO purchase_orders (id, po_number, vendor_id, issued_on) VALUES "
                   f"({n}, {q(po['po_number'])}, {po['vendor_id']}, {q(po['issued_on'])});")
        for ln, l in enumerate(po["lines"], start=1):
            sql.append(f"INSERT INTO po_lines VALUES ({n}, {ln}, {q(l['sku'])}, {q(l['description'])}, {l['qty']}, {l['unit_price']});")
    for n, dn in enumerate(dns, start=1):
        sql.append(f"INSERT INTO delivery_notes (id, dn_number, po_id, received_on) VALUES ({n}, {q(dn['dn_number'])}, {n}, {q(dn['received_on'])});")
        for l in dn["lines"]:
            sql.append(f"INSERT INTO dn_lines VALUES ({n}, {q(l['sku'])}, {l['qty_received']});")
    sql += ["SELECT setval('vendors_id_seq', (SELECT max(id) FROM vendors));",
            "SELECT setval('purchase_orders_id_seq', (SELECT max(id) FROM purchase_orders));",
            "SELECT setval('delivery_notes_id_seq', (SELECT max(id) FROM delivery_notes));", "COMMIT;"]
    (OUT / "seed.sql").write_text("\n".join(sql) + "\n")
    cases = {}
    for l in labels:
        cases[l["case"]] = cases.get(l["case"], 0) + 1
    print(f"{len(labels)} invoices, {len(scan_ids)} as scans. Cases: {cases}")


if __name__ == "__main__":
    main()
