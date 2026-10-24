# Automation Lab

Production-style AI automations, built on n8n with the heavy lifting in Python, all running on one shared platform. Each build solves a real business process end to end, is measured against a labelled test set, and can be tried live.

**Try it:** [Close Desk live demo](https://lab-api-z3mx.onrender.com/). Free hosting, so the first run after a quiet spell takes up to a minute to wake.

| Build | What it does | Status |
| --- | --- | --- |
| **Close Desk** | Invoices to the ledger with three-way matching and fraud checks | [Live](https://lab-api-z3mx.onrender.com/) |
| Speed-to-Lead | Researched reply and CRM record within 2 minutes of a new lead | Planned |
| Fintech Support Desk | WhatsApp and web support agent with a public eval report | Planned |
| FleetPulse | Trotro fleet run from drivers' voice notes in Twi and English | Planned |

## How it fits together

```
Channels (email, demo site, WhatsApp, Slack)
        |
        v
n8n (Render)   --->  Lab API (FastAPI on Render)  --->  Claude
  routing,             extraction, rules,
  approvals,           run log, rate limits
  error alerts                |
                              v
                     Postgres (Neon)
```

n8n orchestrates and handles people (approvals, alerts). The API does the work that should be versioned, tested and explainable. Every run writes to one log, which powers the live timeline on the demo site and a public status endpoint.

## Close Desk

A finance team forwards invoices. Each one is read, checked and either posted to the ledger, sent for approval, or held as possible fraud.

1. **Read.** A PDF with a text layer is sent to Claude as text (cheap). Scans and photos go through vision. Claude answers through a tool schema, so output always has the same shape, with a confidence score per field.
2. **Check.** Plain Python rules, no model involved, so every decision can be explained:
   - bank account differs from the one on file (fraud hold)
   - duplicate: same invoice number after normalising (`INV-0042`, `inv/042`, `INV 42`), same file, or same total within 14 days (fraud hold)
   - line arithmetic, subtotal and tax add up
   - purchase order exists and belongs to this vendor
   - price above the PO by more than 2%
   - quantity above what the delivery notes say was received
   - any field the extractor was unsure about
3. **Decide.** Fraud failures are held and need a written reason to release. Other failures go to an approver in Slack. Clean invoices post double-entry lines to the ledger.
4. **Record.** Invoice, lines, every check, ledger entries, approvals and an audit trail, in one transaction under a lock so two parallel runs can't both post the same invoice.

### Test set

`data/closedesk/generate.py` builds 12 vendors, 40 purchase orders with delivery notes, and 50 invoices in three different layouts, 12 of them turned into phone-photo scans. Ten are planted problems: 3 changed bank accounts, 3 resubmitted duplicates, 2 prices above the PO, 2 quantities above what was received. The generator is seeded, so the files are identical on every run. They aren't committed: run it locally, and the Docker build runs it too (it also writes `labels.json` and the seed migration).

| Eval | Result |
| --- | --- |
| Rules only (perfect extraction) | 50/50 correct decisions, 10/10 planted cases caught, 40/40 clean invoices auto-posted |
| End to end with Claude | Pending: runs once the API key is set |

Run it: `python evals/closedesk_eval.py --mode truth` or `--mode model`.

## Shared platform

- **Run log** with steps, timings and cost per run (`runs`, `run_steps`)
- **Idempotency keys** so the same email or file is never processed twice
- **Rate limits** per visitor and per demo per day, and a **spend cap** that pauses all demos at 90% of the monthly budget
- **Error workflow** in n8n that alerts Slack with the failing node and a link to the execution
- Public `GET /v1/runs/{id}` (live timeline) and `GET /v1/stats` (30-day success rates)

## Run locally

```
pip install -r requirements-dev.txt
createdb lab && export DATABASE_URL=postgresql://localhost/lab LAB_API_KEY=dev
uvicorn lab.main:app --reload          # migrations run on start
pytest                                  # needs a lab_test database
```

## Deploy

Everything runs on free tiers. Render (Blueprint in `render.yaml`) runs n8n and the Lab API. Neon's free Postgres holds both: n8n in its own `n8n` schema, the API in `public`. Both Render services sleep when idle and wake on the next request, which also lets Neon suspend its compute. Set the variables in `.env.example` in the Render dashboard. Import `n8n/closedesk_demo.json` and `n8n/error_handler.json` into n8n, add a Header Auth credential named "Lab API key" (`X-Lab-Key`), and set the API URL in the Config node.
