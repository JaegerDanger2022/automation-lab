"""The semantic layer: what the business means by its words, written down once.

Without this, a model writes SQL that runs and looks right but is wrong: it counts
voided invoices, adds dollars to cedis, or reads "this year" as January to December.
Every definition here is checked by the eval set.
"""
from __future__ import annotations

AS_OF = "2026-10-15"          # the data snapshot date; "today" for every question

TABLES = {
    "erp.brnch": "Branches. br_cd (ACC Accra Central, TML Tema Industrial, KSI Kumasi Adum, TKD Takoradi Harbour), br_nm, rgn (region: Greater Accra, Ashanti, Western).",
    "erp.slsrep": "Sales reps. rep_cd, rep_nm, br_cd, actv ('Y'/'N').",
    "erp.custmst": "Customers. cust_cd, cust_nm (free text), seg_cd (RT retail, WS wholesale, GV government, NG NGO), br_cd (home branch), rep_cd, crdt_lmt (credit limit, GHS), actv_flg ('Y'/'N'), crt_dt.",
    "erp.catmst": "Product categories. cat_cd (BEV, GRN, OIL, CLN, PKG, PER), cat_desc.",
    "erp.itmmst": "Products. itm_cd, itm_desc, cat_cd, uom, std_cost (unit cost, GHS).",
    "erp.arinv": "Sales invoice headers. inv_no, cust_cd, br_cd (selling branch), inv_dt, due_dt, curr_cd ('GHS'/'USD'), fx_rt (GHS per unit of curr_cd on inv_dt, 1 for GHS), net_amt and tax_amt (invoice currency), sts ('O' open, 'P' paid, 'V' void), del_flg (1 = deleted).",
    "erp.arinvln": "Invoice lines. inv_no, ln_no, itm_cd, qty, unit_prc (invoice currency), disc_pct (percent).",
    "erp.arpmt": "Customer payments received. pmt_id, cust_cd, pmt_dt, curr_cd, fx_rt (GHS per unit on pmt_dt), amt (payment currency), mthd ('MOMO' mobile money, 'BANK', 'CHQ' cheque, 'CASH'), ref.",
    "erp.aralloc": "Which invoices a payment paid. pmt_id, inv_no, alloc_amt (invoice currency).",
}

RULES = [
    "Valid invoices only: every query on erp.arinv must include sts <> 'V' AND del_flg = 0, unless the user explicitly asks about voided or deleted invoices.",
    "Money is reported in GHS. Convert invoice amounts with * fx_rt and payment amounts with * fx_rt. Never add USD and GHS amounts directly.",
    "Round money to 2 decimal places in the final SELECT.",
    "The fiscal year runs 1 July to 30 June and is named by the year it ends: FY2026 = 2025-07-01 to 2026-06-30. FY2027 is the current fiscal year.",
    "Fiscal quarters: Q1 Jul-Sep, Q2 Oct-Dec, Q3 Jan-Mar, Q4 Apr-Jun. 'This quarter' is FY2027 Q2 (2026-10-01 to 2026-12-31).",
    "Today is " + AS_OF + ". Use DATE '" + AS_OF + "' instead of CURRENT_DATE or now(). 'This month' is October 2026. 'Last month' is September 2026. 'Year to date' means fiscal year to date: 2026-07-01 to today.",
    "Sales by branch use erp.arinv.br_cd (where the sale was made), not the customer's home branch, unless the user asks about customers' home branches.",
    "Region comes from erp.brnch.rgn joined on the selling branch.",
    "Customer names are free text and some businesses appear twice. When ranking customers, group by cust_cd and show cust_nm; do not merge customers yourself.",
    "Only use the tables listed. Only write a single SELECT (WITH ... SELECT is fine). No writes, no other schemas.",
    "Only add LIMIT when the question asks for it: 'top N' means LIMIT N, and 'which ... the most / highest / lowest' means LIMIT 1. Otherwise return every row (the system caps output at 200). Give columns clear aliases.",
    "Products are listed under PRODUCT CATALOG. Match the user's wording to an itm_cd there (e.g. '25kg rice' is GRN-001) instead of asking which product they mean.",
]

METRICS = {
    "revenue / sales / turnover": "SUM(i.net_amt * i.fx_rt) over valid invoices, by inv_dt. Excludes tax.",
    "gross sales / sales incl. tax / invoiced amount": "SUM((i.net_amt + i.tax_amt) * i.fx_rt) over valid invoices.",
    "worth / value of invoices (including voided ones)": "SUM(i.net_amt * i.fx_rt), excluding tax, like revenue. Include tax only if the user says invoiced amount, gross or including tax.",
    "invoice count / number of invoices": "COUNT(*) of valid invoices.",
    "average invoice value": "revenue / invoice count (excl. tax).",
    "units / quantity sold": "SUM(l.qty) from erp.arinvln joined to valid invoices.",
    "line revenue (for product or category questions)": "SUM(l.qty * l.unit_prc * (1 - l.disc_pct/100) * i.fx_rt) joined to valid invoices. Use this for anything split by product or category.",
    "cost of sales / COGS": "SUM(l.qty * t.std_cost) with erp.itmmst t on itm_cd, valid invoices.",
    "gross margin": "line revenue minus cost of sales. Gross margin % = margin / line revenue * 100.",
    "collections / cash received": "SUM(p.amt * p.fx_rt) from erp.arpmt by pmt_dt.",
    "outstanding / receivables / AR / amount owed": "For valid invoices with sts = 'O': SUM((i.net_amt + i.tax_amt - COALESCE(a.paid, 0)) * i.fx_rt), where a.paid is SUM(alloc_amt) per inv_no from erp.aralloc. Only count allocations, never all payments.",
    "overdue": "outstanding where due_dt < DATE '" + AS_OF + "'. Days overdue = DATE '" + AS_OF + "' - due_dt.",
    "ageing buckets": "by days overdue: Current (not yet due), 1-30, 31-60, 61-90, 90+.",
    "DSO (days sales outstanding)": "outstanding today / revenue incl. tax over the last 90 days * 90.",
    "active customers": "custmst.actv_flg = 'Y'. 'Customers who bought' means distinct cust_cd with a valid invoice in the period.",
    "over credit limit": "customer outstanding (GHS) > custmst.crdt_lmt.",
    "growth / vs last year": "compare the same period one fiscal year earlier, e.g. FY2027 Q1 vs FY2026 Q1.",
}

EXAMPLES = [
    ("What was revenue last fiscal year?",
     "SELECT ROUND(SUM(i.net_amt * i.fx_rt), 2) AS revenue_ghs FROM erp.arinv i "
     "WHERE i.sts <> 'V' AND i.del_flg = 0 AND i.inv_dt BETWEEN DATE '2025-07-01' AND DATE '2026-06-30'"),
    ("How much do government customers owe us?",
     "WITH a AS (SELECT inv_no, SUM(alloc_amt) AS paid FROM erp.aralloc GROUP BY inv_no) "
     "SELECT ROUND(SUM((i.net_amt + i.tax_amt - COALESCE(a.paid, 0)) * i.fx_rt), 2) AS outstanding_ghs "
     "FROM erp.arinv i JOIN erp.custmst c ON c.cust_cd = i.cust_cd LEFT JOIN a ON a.inv_no = i.inv_no "
     "WHERE i.sts = 'O' AND i.del_flg = 0 AND c.seg_cd = 'GV'"),
]

OUT_OF_SCOPE = [
    "anything not in these tables: payroll, expenses, inventory on hand, budgets, forecasts, suppliers, purchases, bank balances",
    "advice or opinions (should we fire a rep, which customer to drop)",
    "personal data beyond names already in the tables",
]


PRODUCTS = "BEV-001 Bottled water 500ml x24; BEV-002 Malt drink 330ml x24; BEV-003 Orange juice 1L x12; BEV-004 Energy drink 250ml x24; GRN-001 Rice 25kg; GRN-002 Rice 5kg; GRN-003 Wheat flour 50kg; GRN-004 Maize 50kg; GRN-005 Spaghetti 500g x20; OIL-001 Vegetable oil 5L; OIL-002 Vegetable oil 25L; OIL-003 Palm oil 4L; CLN-001 Laundry soap bar x40; CLN-002 Detergent powder 5kg; CLN-003 Bleach 1L x12; CLN-004 Dishwashing liquid 1L x12; PKG-001 Carrier bags x1000; PKG-002 Takeaway packs x500; PKG-003 Shrink wrap roll; PER-001 Toothpaste 100ml x48; PER-002 Body lotion 400ml x24; PER-003 Sanitary pads x48; PER-004 Tissue roll x48"
CATEGORIES = "BEV Beverages; GRN Grains and flour; OIL Cooking oil; CLN Cleaning products; PKG Packaging; PER Personal care"


def prompt_block() -> str:
    lines = [f"Data is as of {AS_OF}.", "", "TABLES"]
    lines += [f"- {k}: {v}" for k, v in TABLES.items()]
    lines += ["", "BUSINESS DEFINITIONS (always use these)"]
    lines += [f"- {k}: {v}" for k, v in METRICS.items()]
    lines += ["", "PRODUCT CATALOG (itm_cd description)", PRODUCTS, "Categories (cat_cd description): " + CATEGORIES]
    lines += ["", "RULES"] + [f"- {r}" for r in RULES]
    lines += ["", "OUT OF SCOPE (say you can't answer)"] + [f"- {o}" for o in OUT_OF_SCOPE]
    lines += ["", "EXAMPLES"] + [f"Q: {qq}\nSQL: {s}" for qq, s in EXAMPLES]
    return "\n".join(lines)
