"""Checks every query before it touches the database, then runs it read-only.

Three layers, because a prompt alone is not a control:
  1. Parse: one statement, SELECT only, only the allowed ERP tables, no dangerous functions.
  2. Business lint: the mistakes that give a plausible but wrong answer (voided invoices
     counted, dollars added to cedis, the server clock used as "today").
  3. Run inside a READ ONLY transaction with a timeout and a row cap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import sqlglot
from sqlglot import exp

from .semantic import TABLES

ALLOWED = {t.split(".")[1] for t in TABLES}
BLOCKED_FUNCS = {"pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "dblink", "set_config",
                 "current_setting", "lo_import", "lo_export", "pg_terminate_backend", "pg_cancel_backend",
                 "query_to_xml", "copy"}
ROW_CAP = 200
TIMEOUT_MS = 5000


@dataclass
class Verdict:
    ok: bool
    errors: list[str] = field(default_factory=list)     # must fix before running
    warnings: list[str] = field(default_factory=list)   # shown to the model for repair
    tables: list[str] = field(default_factory=list)


def check(sql: str, question: str = "") -> Verdict:
    v = Verdict(ok=True)
    try:
        trees = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as e:
        return Verdict(ok=False, errors=[f"SQL does not parse: {str(e).splitlines()[0]}"])
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        return Verdict(ok=False, errors=["Send exactly one statement."])
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return Verdict(ok=False, errors=["Only SELECT queries are allowed."])
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command)):
            v.errors.append("Only SELECT queries are allowed.")
            break

    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in ctes and not t.db:
            continue
        if t.db and t.db.lower() != "erp":
            v.errors.append(f"Schema {t.db} is not allowed. Use erp.")
        elif name not in ALLOWED:
            v.errors.append(f"Table {name} is not allowed.")
        else:
            v.tables.append(name)
    for f in tree.find_all(exp.Func):
        fname = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).lower()
        if fname in BLOCKED_FUNCS:
            v.errors.append(f"Function {fname} is not allowed.")

    low = re.sub(r"\s+", " ", sql.lower())
    q = question.lower()
    if re.search(r"\bcurrent_date\b|\bnow\s*\(|\bcurrent_timestamp\b|\blocaltimestamp\b", low):
        v.errors.append("Use the data date DATE '2026-10-15' instead of CURRENT_DATE or now().")
    if "arinv" in v.tables and not re.search(r"\b(void|cancel|deleted)", q):
        if "sts" not in low:
            v.errors.append("Invoices must exclude voided ones: add sts <> 'V' (or sts = 'O' for open items).")
        if "del_flg" not in low:
            v.errors.append("Invoices must exclude deleted ones: add del_flg = 0.")
    money = re.search(r"\b(net_amt|tax_amt|alloc_amt|unit_prc)\b|\bamt\b", low)
    if money and "fx_rt" not in low and "sum(" in low and not re.search(r"\b(usd|dollar|ghs only|cedi)", q):
        v.errors.append("Amounts are in mixed currencies: multiply by fx_rt to report GHS.")
    v.tables = sorted(set(v.tables))
    v.ok = not v.errors
    return v


def run(conn, sql: str) -> tuple[list[str], list[list]]:
    """Runs a checked query read-only. Returns column names and rows (max ROW_CAP)."""
    capped = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS q LIMIT {ROW_CAP}"
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = {TIMEOUT_MS}")
        conn.execute("SET LOCAL search_path = erp")
        cur = conn.execute(capped)
        cols = [c.name for c in cur.description]
        rows = [[_plain(x) for x in r] for r in cur.fetchall()]
    return cols, rows


def _plain(x):
    if isinstance(x, Decimal):
        return float(x)
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x
