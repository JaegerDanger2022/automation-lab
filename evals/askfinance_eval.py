"""Ask Finance evaluation.

  python evals/askfinance_eval.py --mode gold    # checks the gold SQL runs and passes the guard (no model)
  python evals/askfinance_eval.py --mode model   # the agent answers every question; results compared to gold

A SQL answer counts as correct when its result contains every gold column (matched by
values, so column names and extra columns don't matter) with the same number of rows.
Numbers match within 0.5% unless the case sets its own tolerance.
Writes evals/results/askfinance_<mode>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from askfinance_questions import CASES  # noqa: E402
from lab.askfinance import sqlguard  # noqa: E402


def _norm(v, tol):
    if isinstance(v, (int, float)):
        return ("n", float(v))
    if v is None:
        return ("x", None)
    return ("s", str(v).strip().lower())


def _col_match(gold_col, cand_col, tol, ordered):
    g = [_norm(v, tol) for v in gold_col]
    c = [_norm(v, tol) for v in cand_col]
    if not ordered:
        g, c = sorted(g, key=str), sorted(c, key=str)
    if len(g) != len(c):
        return False
    for a, b in zip(g, c):
        if a[0] != b[0]:
            # allow numbers stored as text and dates vs date-times
            if a[0] == "s" and b[0] == "s":
                pass
            else:
                return False
        if a[0] == "n":
            if abs(a[1] - b[1]) > max(abs(a[1]) * tol, 0.011):
                return False
        elif a[0] == "s" and not (a[1] == b[1] or b[1].startswith(a[1]) or a[1].startswith(b[1])):
            return False
    return True


def results_match(gold_cols, gold_rows, cols, rows, tol=0.005):
    if len(gold_rows) != len(rows):
        return False
    gt = list(zip(*gold_rows)) if gold_rows else [[] for _ in gold_cols]
    ct = list(zip(*rows)) if rows else [[] for _ in cols]
    used = set()
    for g in gt:
        hit = next((j for j, c in enumerate(ct) if j not in used and
                    (_col_match(g, c, tol, ordered=False))), None)
        if hit is None:
            return False
        used.add(hit)
    return True


def evaluate(conn, mode: str = "gold", agent=None, cases=None, log=print) -> dict:
    cases = cases or CASES
    rows_out, cost = [], 0.0
    for case in cases:
        t0 = time.time()
        rec = {"id": case["id"], "cat": case["cat"], "q": case["q"]}
        gold = None
        if "gold" in case:
            v = sqlguard.check(case["gold"], case["q"])
            gold = sqlguard.run(conn, case["gold"])
            rec.update({"gold_guard_ok": v.ok, "gold_guard_errors": v.errors, "gold_rows": len(gold[1])})
        if mode == "gold":
            rec["correct"] = rec.get("gold_guard_ok", True)
        else:
            try:
                res = agent.ask(conn, case["q"])
            except Exception as e:                       # count provider errors as failures, keep going
                rec.update({"status": "error", "answer": f"{type(e).__name__}: {e}", "correct": False,
                            "repairs": 0, "cost_usd": 0, "seconds": round(time.time() - t0, 2)})
                rows_out.append(rec)
                log(f"ERR  {case['id']:4} {e}")
                continue
            cost += res.cost_usd
            rec.update({"status": res.status, "sql": res.sql, "answer": res.answer, "model": res.model,
                        "repairs": sum(1 for s in res.steps if s["name"] == "Repair"),
                        "cost_usd": round(res.cost_usd, 5)})
            if gold:
                rec["correct"] = res.status == "answered" and results_match(
                    gold[0], gold[1], res.columns, res.rows, case.get("tolerance", 0.005))
            else:
                want = case["expect"]
                rec["correct"] = res.status == ("clarify" if want == "clarify" else "refused") or (
                    want == "clarify" and res.status == "refused")
        rec["seconds"] = round(time.time() - t0, 2)
        rows_out.append(rec)
        log(f"{'PASS' if rec['correct'] else 'FAIL'} {case['id']:4} {case['q'][:70]}")

    by_cat = Counter(r["cat"] for r in rows_out)
    ok_cat = Counter(r["cat"] for r in rows_out if r["correct"])
    summary = {"mode": mode, "cases": len(rows_out), "correct": sum(r["correct"] for r in rows_out),
               "accuracy": round(sum(r["correct"] for r in rows_out) / len(rows_out), 3),
               "by_category": {k: f"{ok_cat[k]}/{n}" for k, n in sorted(by_cat.items())}}
    if mode == "model":
        summary.update({"cost_usd": round(cost, 4), "repairs": sum(r["repairs"] for r in rows_out),
                        "median_seconds": sorted(r["seconds"] for r in rows_out)[len(rows_out) // 2],
                        "model": agent.model, "fallback": agent.fallback})
    return {"summary": summary, "rows": rows_out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["gold", "model"], default="gold")
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost/lab"))
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    agent = None
    if args.mode == "model":
        from lab.askfinance.agent import Agent
        agent = Agent()
    with psycopg.connect(args.db, autocommit=True) as conn:
        result = evaluate(conn, args.mode, agent, cases)
    out = ROOT / "evals" / "results"
    out.mkdir(exist_ok=True)
    (out / f"askfinance_{args.mode}.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
