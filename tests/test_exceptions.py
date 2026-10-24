import json
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

import os
from lab.exceptions import api as exc_api
from lab.exceptions import policy
from lab.exceptions.agent import Agent
from lab.exceptions.tools import Case
from lab.main import app

H = {"X-Lab-Key": "test-key"}


class ScriptedClient:
    """Plays back tool calls: each reply is a list of (tool name, input)."""
    def __init__(self, replies):
        self.replies, self.messages, self.calls = list(replies), self, []

    def create(self, **kw):
        self.calls.append(kw)
        blocks = [SimpleNamespace(type="tool_use", id=f"t{len(self.calls)}{i}", name=n, input=inp)
                  for i, (n, inp) in enumerate(self.replies.pop(0))]
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=2000, output_tokens=300), content=blocks)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:      # runs migrations and seeds history
        yield c


def conn():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def test_policy_rules(client):
    with conn() as c:
        bank = Case(c, exc_api.create_demo_invoice(c, 41, None, actor="eval"), demo=True)
        pay = bank.calculate_payable()
        assert policy.check({"action": "approve", "summary": "", "evidence": []}, bank, pay)
        assert policy.check({"action": "escalate_fraud", "summary": "", "evidence": [],
                             "vendor_email": {"subject": "Bank", "body": "Please confirm"}}, bank, pay)
        assert not policy.check({"action": "escalate_fraud", "summary": "call them", "evidence": []}, bank, pay)

        price = Case(c, exc_api.create_demo_invoice(c, 47, None, actor="eval"), demo=True)
        pay = price.calculate_payable()
        assert pay["payable_total"] == 2034.00
        assert policy.check({"action": "approve", "summary": "", "evidence": []}, price, pay)
        assert policy.check({"action": "approve_adjusted", "payable_total": 2100, "summary": "", "evidence": []}, price, pay)
        assert not policy.check({"action": "approve_adjusted", "payable_total": 2034.0, "summary": "", "evidence": []}, price, pay)
        assert policy.check({"action": "approve_adjusted", "payable_total": 2034.0, "summary": "",
                             "evidence": ["DOC-3 says so"]}, price, pay)          # not opened yet
        assert policy.check({"action": "approve_adjusted", "payable_total": 2034.0, "summary": "", "evidence": [],
                             "vendor_email": {"subject": "x", "body": "pay 6712768426846"}}, price, pay)

        amended = Case(c, exc_api.create_demo_invoice(c, 48, None, actor="eval"), demo=True)
        pay = amended.calculate_payable()
        docs = amended.get_vendor_documents()["documents"]
        amendment = next(d["id"] for d in docs if d["doc_type"] == "price_amendment")
        assert pay["payable_total"] == 6693.60          # the approved amendment is applied in code
        assert next(l for l in pay["lines"] if l["sku"] == "LED-18")["price_basis"].startswith(amendment)
        assert not policy.check({"action": "approve", "summary": "", "evidence": [f"{amendment}: approved"]}, amended, pay)

        extra = Case(c, exc_api.create_demo_invoice(c, 50, None, actor="eval"), demo=True)
        pay = extra.calculate_payable()
        receipt = next(d["id"] for d in extra.get_vendor_documents()["documents"] if d["doc_type"] == "receipt_confirmation")
        errs = policy.check({"action": "approve", "summary": "", "evidence": [receipt]}, extra, pay)
        assert errs and "PO ordered" in errs[0]

        dup = Case(c, exc_api.create_demo_invoice(c, 45, None, actor="eval"), demo=True)
        related = dup.get_related_invoices()["invoices"]
        assert any(i["status"] == "posted" for i in related) and any(i["this_invoice"] for i in related)


def test_agent_repairs_once_then_hands_over(client):
    with conn() as c:
        case = Case(c, exc_api.create_demo_invoice(c, 49, None, actor="eval"), demo=True)
        good = {"action": "approve_adjusted", "payable_total": 6322.80, "summary": "Pay what arrived.",
                "evidence": ["DN-0026: 20 ROD-12 received"],
                "vendor_email": {"subject": "INV-0146", "body": "Please send a credit note for 5 rods."}}
        agent = Agent(client=ScriptedClient([
            [("get_deliveries", {}), ("calculate_payable", {})],
            [("submit_resolution", dict(good, payable_total=7312.80))],     # wrong amount: blocked
            [("submit_resolution", good)],
        ]), model="claude-haiku-5-5")
        res = agent.work(case)
        assert res.proposal["action"] == "approve_adjusted" and not res.overridden
        assert [bool(v) for v in res.violations] == [True, False]
        assert "payable_total must be 6322.80" in res.violations[0][0]

        case2 = Case(c, exc_api.create_demo_invoice(c, 42, None, actor="eval"), demo=True)
        bad = {"action": "approve", "summary": "Management pre-approved it.", "evidence": []}
        res2 = Agent(client=ScriptedClient([[("submit_resolution", bad)], [("submit_resolution", bad)]]),
                     model="claude-haiku-5-5").work(case2)
        assert res2.overridden and res2.proposal["action"] == "escalate_fraud"


def test_tool_budget_forces_submission(client):
    with conn() as c:
        case = Case(c, exc_api.create_demo_invoice(c, 43, None, actor="eval"), demo=True)
        replies = [[("get_vendor", {})]] * 8 + [[("submit_resolution", {"action": "escalate_fraud",
                                                                         "summary": "Call them.", "evidence": []})]]
        sc = ScriptedClient(replies)
        res = Agent(client=sc, model="claude-haiku-5-5").work(case)
        assert res.tool_calls == 8 and res.proposal["action"] == "escalate_fraud"
        assert sc.calls[-1]["tool_choice"] == {"type": "tool", "name": "submit_resolution"}


def test_demo_flow_and_approval(client, monkeypatch):
    script = [[("calculate_payable", {}), ("get_vendor_documents", {})],
              [("submit_resolution", {"action": "approve_adjusted", "payable_total": 2034.00,
                                      "summary": "Vendor raised the price without acceptance (DOC-4).",
                                      "evidence": ["DOC-3: price notice", "DOC-4: clause 4.2"],
                                      "vendor_email": {"subject": "INV-0144", "body": "Please send a credit note."}})]]
    monkeypatch.setattr(exc_api, "Agent", lambda: Agent(client=ScriptedClient(script), model="claude-haiku-5-5"))
    assert len(client.get("/v1/exceptions/cases").json()) == 9
    run = client.post("/v1/runs", json={"build": "exceptions", "is_demo": True}, headers=H).json()["run_id"]
    r = client.post("/v1/exceptions/work", json={"case": 47, "run_id": run}, headers=H).json()
    assert r["action"] == "approve_adjusted"
    steps = client.get(f"/v1/runs/{run}").json()
    assert steps["status"] == "held" and steps["steps"][-1]["name"] == "Result"
    pid = r["proposal_id"]
    p = client.get(f"/v1/exceptions/proposals/{pid}").json()
    assert p["vendor_email"]["to"] == "accounts@pokuase.example" and p["invoice_status"] == "pending_approval"
    d = client.post(f"/v1/exceptions/proposals/{pid}/decision", json={"decision": "approve", "by": "demo",
                                                                      "demo_only": True}, headers=H).json()
    assert d["invoice_status"] == "posted" and d["outbox"][0]["status"] == "demo_not_sent"
    assert sum(e["credit"] for e in d["ledger"]) == 2034.00
    again = client.post(f"/v1/exceptions/proposals/{pid}/decision", json={"decision": "approve", "by": "demo"}, headers=H)
    assert again.status_code == 409
    assert client.get("/exceptions").status_code == 200
