import json
from pathlib import Path
from fastapi.testclient import TestClient
from lab.main import app

H = {"X-Lab-Key": "test-key"}
LABELS = json.loads((Path(__file__).resolve().parents[1] / "data/closedesk/labels.json").read_text())

def test_platform_and_closedesk_flow():
    with TestClient(app) as c:
        assert c.get("/health").json() == {"ok": True}
        assert c.post("/v1/runs", json={"build": "closedesk"}).status_code == 401
        r = c.post("/v1/runs", json={"build": "closedesk", "idempotency_key": "email-1", "is_demo": True,
                                     "visitor": "1.2.3.4"}, headers=H).json()
        assert r["duplicate"] is False
        run_id = r["run_id"]
        assert c.post("/v1/runs", json={"build": "closedesk", "idempotency_key": "email-1"}, headers=H).json()["duplicate"]
        c.post(f"/v1/runs/{run_id}/steps", json={"name": "extract", "status": "ok"}, headers=H)

        clean = next(l for l in LABELS if l["case"] == "clean")
        body = {"invoice": dict(clean["truth"], confidence={}), "file_sha256": "sha-1", "run_id": run_id}
        rec = c.post("/v1/closedesk/record", json=body, headers=H).json()
        assert rec["decision"] == "posted"
        # Same file again is a duplicate and held.
        again = c.post("/v1/closedesk/record", json=body, headers=H).json()
        assert again["decision"] == "held"
        # Releasing a fraud hold without a reason is refused.
        d = c.post(f"/v1/closedesk/invoices/{again['invoice_id']}/decision", json={"decision": "approve", "by": "martin"}, headers=H)
        assert d.status_code == 409
        d = c.post(f"/v1/closedesk/invoices/{again['invoice_id']}/decision", json={"decision": "reject", "by": "martin"}, headers=H)
        assert d.json()["status"] == "rejected"

        c.post(f"/v1/runs/{run_id}/finish", json={"status": "succeeded", "cost_usd": "0.004", "provider": "anthropic"}, headers=H)
        tl = c.get(f"/v1/runs/{run_id}").json()
        assert tl["status"] == "succeeded" and tl["steps"][0]["name"] == "extract"
        assert any(s["build"] == "closedesk" for s in c.get("/v1/stats").json())
        assert len(c.get("/v1/closedesk/ledger").json()) == 2
        assert len(c.get("/v1/closedesk/samples").json()) == 12
