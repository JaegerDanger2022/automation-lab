"""Close Desk evaluation.

  python evals/closedesk_eval.py --mode truth   # rules only: feeds the labelled values in as if
                                                # extraction were perfect. No API key needed.
  python evals/closedesk_eval.py --mode model   # real run: Claude reads every file, then the rules.

Invoices are processed in order against a fresh database, the way they would arrive,
so duplicates are only caught if the original was seen first.
Writes evals/results/closedesk_<mode>.json and prints a summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.closedesk import repo  # noqa: E402
from lab.closedesk.extract import to_invoice  # noqa: E402
from lab.closedesk.rules import norm_account, norm_name, norm_ref, run_checks  # noqa: E402

DATA = ROOT / "data" / "closedesk"
FIELDS = ["vendor_name", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total", "bank_account"]


def same(field: str, got, want) -> bool:
    if got is None:
        return False
    if field in ("subtotal", "tax", "total"):
        return abs(Decimal(str(got)) - Decimal(want)) <= Decimal("0.01")
    if field == "vendor_name":
        return norm_name(str(got)) == norm_name(want)
    if field in ("invoice_number", "po_number"):
        return norm_ref(str(got)) == norm_ref(want)
    if field == "bank_account":
        return norm_account(str(got)) == norm_account(want)
    return str(got) == str(want)


def reset_db(conn):
    conn.execute("TRUNCATE audit_log, approvals, ledger_entries, invoice_checks, invoice_lines, invoices CASCADE")
    conn.execute((DATA / "seed.sql").read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["truth", "model"], default="truth")
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost/lab"))
    args = ap.parse_args()

    labels = json.loads((DATA / "labels.json").read_text())
    rows, field_hits, field_total = [], {f: 0 for f in FIELDS}, 0
    tokens_in = tokens_out = 0
    with psycopg.connect(args.db, autocommit=True) as conn:
        reset_db(conn)
        for item in labels:
            path = DATA / "invoices" / item["file"]
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            t0 = time.time()
            if args.mode == "truth":
                raw = dict(item["truth"])
                raw["confidence"] = {}
                inv, usage = to_invoice(raw), {"path": "truth"}
            else:
                from lab.closedesk.extract import extract
                media = "application/pdf" if path.suffix == ".pdf" else "image/jpeg"
                inv, usage = extract(data, media)
                tokens_in += usage["input_tokens"]
                tokens_out += usage["output_tokens"]
            report = run_checks(inv, repo.load_master(conn), file_sha256=sha)
            repo.save_invoice(conn, inv, report, sha, run_id=None, actor="eval")
            got = json.loads(inv.model_dump_json())
            fields_ok = {f: same(f, got.get(f), item["truth"][f]) for f in FIELDS}
            for f, ok in fields_ok.items():
                field_hits[f] += ok
            field_total += 1
            rows.append({"file": item["file"], "case": item["case"], "expected": item["expected_decision"],
                         "decision": report.decision, "correct": report.decision == item["expected_decision"],
                         "reasons": report.reasons, "fields_ok": fields_ok, "path": usage["path"],
                         "seconds": round(time.time() - t0, 2)})

    planted = [r for r in rows if r["case"] != "clean"]
    clean = [r for r in rows if r["case"] == "clean"]
    summary = {
        "mode": args.mode,
        "invoices": len(rows),
        "decision_accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3),
        "planted_caught": f"{sum(r['decision'] != 'posted' for r in planted)}/{len(planted)}",
        "planted_correct_route": f"{sum(r['correct'] for r in planted)}/{len(planted)}",
        "clean_auto_posted": f"{sum(r['decision'] == 'posted' for r in clean)}/{len(clean)}",
        "field_accuracy": {f: round(field_hits[f] / field_total, 3) for f in FIELDS},
        "field_accuracy_overall": round(sum(field_hits.values()) / (field_total * len(FIELDS)), 3),
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2],
    }
    if args.mode == "model":
        summary["tokens"] = {"input": tokens_in, "output": tokens_out}
    out = ROOT / "evals" / "results"
    out.mkdir(exist_ok=True)
    (out / f"closedesk_{args.mode}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    misses = [r for r in rows if not r["correct"]]
    for r in misses:
        print(f"MISS {r['file']} case={r['case']} expected={r['expected']} got={r['decision']} {r['reasons']}")


if __name__ == "__main__":
    main()
