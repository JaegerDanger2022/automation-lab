import os
from types import SimpleNamespace

import psycopg
import pytest

from lab.askfinance import sqlguard
from lab.askfinance.agent import Agent

GOOD = "SELECT ROUND(SUM(i.net_amt * i.fx_rt), 2) FROM erp.arinv i WHERE i.sts <> 'V' AND i.del_flg = 0"


@pytest.mark.parametrize("sql,why", [
    ("DELETE FROM erp.arinv", "Only SELECT"),
    ("SELECT 1; DROP TABLE erp.arinv", "exactly one"),
    ("SELECT * FROM public.runs", "not allowed"),
    ("SELECT * FROM pg_catalog.pg_user", "not allowed"),
    ("SELECT pg_sleep(30)", "pg_sleep"),
    ("SELECT ROUND(SUM(net_amt * fx_rt),2) FROM erp.arinv", "voided"),
    ("SELECT SUM(net_amt) FROM erp.arinv WHERE sts <> 'V' AND del_flg = 0", "fx_rt"),
    ("SELECT COUNT(*) FROM erp.arinv WHERE sts <> 'V' AND del_flg = 0 AND inv_dt > CURRENT_DATE - 30", "2026-10-15"),
    ("WITH x AS (DELETE FROM erp.arinv RETURNING *) SELECT * FROM x", "Only SELECT"),
])
def test_guard_blocks(sql, why):
    v = sqlguard.check(sql, "how much revenue")
    assert not v.ok and any(why in e for e in v.errors), v.errors


def test_guard_allows_good_sql():
    assert sqlguard.check(GOOD, "revenue").ok
    assert sqlguard.check("SELECT COUNT(*) FROM erp.arinv WHERE sts = 'V'", "how many voided invoices").ok


def test_read_only_even_if_guard_missed():
    url = os.environ.get("DATABASE_URL")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(open("db/migrations/004_erp.sql").read())
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("CREATE TABLE erp.x (a int)")


class ScriptedClient:
    """Plays back tool calls in order, so the agent loop can be tested without a model."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        data = self.replies.pop(0)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                               content=[SimpleNamespace(type="tool_use", input=data)])


def test_agent_repairs_rejected_sql_then_answers():
    url = os.environ.get("DATABASE_URL")
    client = ScriptedClient([
        {"action": "sql", "sql": "SELECT SUM(net_amt) FROM erp.arinv"},          # wrong: voids + fx
        {"action": "sql", "sql": GOOD, "chart": "none"},
        {"answer": "Revenue was GHS X."},
    ])
    agent = Agent(client=client, model="claude-haiku-5-5", fallback="claude-sonnet-5-5")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(open("db/migrations/004_erp.sql").read())
        res = agent.ask(conn, "What is total revenue?")
    assert res.status == "answered" and res.sql == GOOD
    assert [s["name"] for s in res.steps] == ["Plan", "Guard", "Repair", "Guard", "Run", "Answer"]
    assert "voided" in client.calls[1]["messages"][-1]["content"]


def test_agent_refusal_never_touches_db():
    client = ScriptedClient([{"action": "refuse", "message": "I can only read sales data."}])
    agent = Agent(client=client, model="claude-haiku-5-5")
    res = agent.ask(None, "delete the voided invoices")
    assert res.status == "refused" and res.sql is None


def test_agent_falls_back_to_stronger_model():
    url = os.environ.get("DATABASE_URL")
    bad = {"action": "sql", "sql": "SELECT SUM(net_amt) FROM erp.arinv"}
    client = ScriptedClient([bad, bad, bad, {"action": "sql", "sql": GOOD}, {"answer": "ok"}])
    agent = Agent(client=client, model="claude-haiku-5-5", fallback="claude-sonnet-5-5")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(open("db/migrations/004_erp.sql").read())
        res = agent.ask(conn, "revenue")
    assert res.status == "answered"
    assert client.calls[3]["model"] == "claude-sonnet-5-5"
