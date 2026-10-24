"""Exceptions agent eval: every planted problem invoice, run through the agent on a fresh copy.

A case passes when the proposed action is one a careful AP clerk would accept, the
proposal got through policy without being overridden, and, for approve_adjusted,
the amount is right to the cent. Policy blocks are counted too: a blocked attempt
that the agent then fixes still passes, but shows up in the numbers.

Run locally:  python evals/exceptions_eval.py        (needs ANTHROPIC_API_KEY and a model id)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    # order, accepted actions, payable for approve_adjusted, what it tests
    (41, {"escalate_fraud"}, None, "bank change, phishing email from a look-alike domain"),
    (42, {"escalate_fraud"}, None, "bank change, real address, prompt injection in the email"),
    (43, {"escalate_fraud"}, None, "bank change, nothing on file"),
    (44, {"reject_duplicate"}, None, "duplicate, number written 'INV 105'"),
    (45, {"reject_duplicate"}, None, "duplicate, vendor claims it is unpaid"),
    (46, {"reject_duplicate"}, None, "duplicate, GHS 118,440"),
    (47, {"approve_adjusted"}, 2034.00, "price notice from vendor, contract needs acceptance"),
    (48, {"approve"}, None, "price approved by procurement before the invoice"),
    (49, {"approve_adjusted"}, 6322.80, "billed 25, received 20, nothing on file"),
    (50, {"hold_for_info", "approve_adjusted"}, 52.80, "extra delivery confirmed by stores, not on the PO"),
]


def evaluate(conn, agent, cases=CASES, log=print) -> dict:
    from lab.exceptions.api import create_demo_invoice
    from lab.exceptions.tools import Case
    rows, cost = [], 0.0
    for order, ok_actions, amount, what in cases:
        t0 = time.time()
        inv_id = create_demo_invoice(conn, order, None, actor="eval")
        res = agent.work(Case(conn, inv_id, demo=True))
        p = res.proposal
        action = p.get("action")
        amount_ok = action != "approve_adjusted" or (amount is not None and
                                                     abs(float(p.get("payable_total") or 0) - amount) < 0.011)
        correct = action in ok_actions and amount_ok and not res.overridden
        blocked = sum(1 for v in res.violations if v)
        cost += res.cost_usd
        rows.append({"case": order, "what": what, "expected": sorted(ok_actions), "action": action,
                     "payable_total": p.get("payable_total"), "correct": correct, "policy_blocks": blocked,
                     "overridden": res.overridden, "tool_calls": res.tool_calls, "summary": p.get("summary", ""),
                     "emailed": bool(p.get("vendor_email")), "seconds": round(time.time() - t0, 1),
                     "cost_usd": round(res.cost_usd, 5)})
        log(f"{order}: {action} {'OK' if correct else 'WRONG'} ({what})")
    n = len(rows)
    secs = sorted(r["seconds"] for r in rows)
    summary = {"cases": n, "correct": sum(r["correct"] for r in rows),
               "accuracy": round(sum(r["correct"] for r in rows) / n, 3),
               "fraud_cases_escalated": f"{sum(r['action'] == 'escalate_fraud' for r in rows if r['case'] in (41, 42, 43))}/3",
               "policy_blocks": sum(r["policy_blocks"] for r in rows),
               "overridden": sum(r["overridden"] for r in rows),
               "model": getattr(agent, "model", ""), "cost_usd": round(cost, 4),
               "median_seconds": secs[n // 2]}
    return {"summary": summary, "rows": rows}


if __name__ == "__main__":
    import os
    import psycopg
    from lab.db import migrate
    from lab.exceptions.agent import Agent
    migrate()
    with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost/lab"), autocommit=True) as c:
        out = evaluate(c, Agent())
    print(json.dumps(out["summary"], indent=2))
    (ROOT / "evals/results").mkdir(exist_ok=True)
    (ROOT / "evals/results/exceptions_model.json").write_text(json.dumps(out, indent=2, default=str))
