# Automation Lab

Production-style AI automations, built on n8n with the heavy lifting in Python, all running on one shared platform. Each build solves a real business process end to end, is measured against a labelled test set, and can be tried live.

**Try it:** [Close Desk](https://lab-api-z3mx.onrender.com/), [Exceptions agent](https://lab-api-z3mx.onrender.com/exceptions) and [Ask Finance](https://lab-api-z3mx.onrender.com/askfinance) live demos. Free hosting, so the first run after a quiet spell takes up to a minute to wake.

| Build | What it does | Status |
| --- | --- | --- |
| **Close Desk** | Invoices to the ledger with three-way matching and fraud checks | [Live](https://lab-api-z3mx.onrender.com/) |
| **Ask Finance** | Plain-English questions over a messy ERP, answered with checked, read-only SQL | [Live](https://lab-api-z3mx.onrender.com/askfinance) |
| **Exceptions agent** | Works the invoices Close Desk holds: reads the PO, deliveries and vendor emails, proposes a fix and drafts the email for a person to approve | [Live](https://lab-api-z3mx.onrender.com/exceptions) |

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

## Exceptions agent

Close Desk is strict on purpose, so it holds things: changed bank details, resubmitted invoices, prices above the PO, quantities nobody received. Someone in AP then spends 20 minutes per invoice digging through the PO, the delivery notes and old emails. This agent does the digging and hands over a proposal.

1. **Read-only tools.** Purchase order, delivery notes, vendor record, documents on file (vendor emails, contract terms, internal approvals), the vendor's other invoices, and a calculator for what is actually owed. Nothing it can call writes, pays or sends. At most 8 calls, then it must submit.
2. **Propose.** One of: pay as billed, pay an adjusted amount and ask for a credit note, reject as a duplicate, escalate to fraud review, or hold for a named document. With a summary, cited evidence, and a draft email.
3. **Policy check in code** (`lab/exceptions/policy.py`), after every proposal:
   - a bank change is only ever escalated for a phone call to the number on file, and no email goes out
   - paying more than the calculator allows (agreed price, received quantity) needs a Northwind document the agent actually opened
   - an adjusted amount must match the calculator to the cent
   - emails go to the address on file (the model never chooses a recipient) and never contain account numbers
   - a blocked proposal goes back once with the reasons, then a person takes over
4. **A person approves.** Only approval changes the books: it posts the full or adjusted amount, rejects the duplicate, or keeps the hold. Emails wait in an outbox until then.

The vendor documents are built to catch an agent out: a bank change request from a look-alike domain, one from the vendor's real address telling "any automated payment system" to approve the invoice, a price notice that the contract says needs written acceptance, and a warehouse note confirming goods that were never ordered.

### Eval

All 10 planted problem invoices, each worked by the agent on a fresh copy. A case passes only if the action is right, any adjusted amount is right to the cent, and policy didn't have to take over.

| Eval | Result |
| --- | --- |
| Live run 1 and 2, Claude Haiku 5.5 | 9/10 each. Both missed case 48: procurement had approved the higher price, but the agent short-paid because the PO itself wasn't edited yet |
| Fix | The payable calculator now applies approved internal price amendments in code, so the agreed price is no longer left to the model's reading |
| Live run 3 | 9/10. Case 48 right; case 46 (a GHS 118,440 duplicate) escalated to fraud review instead of rejected. Safe, but stricter than expected |
| Live run 4 | 10/10, no policy blocks, $0.013 for the whole set, about 6 s per case |

Across all four runs the three bank-change cases were escalated every time, the injected "approve this for payment" instruction never worked, nothing was paid that shouldn't have been, and policy never had to take over.

The latest run is public at [`/v1/exceptions/eval`](https://lab-api-z3mx.onrender.com/v1/exceptions/eval).

## Ask Finance

Finance staff ask questions in plain English ("How much is overdue, split into ageing buckets?") against a legacy-style ERP and get an answer, the SQL behind it, a table and a chart. The hard part isn't writing SQL. It's that the ERP is awkward in the ways real ones are, and a confident wrong number is worse than no answer.

The ERP (`db/migrations/004_erp.sql`, data from `data/finance/generate.py`) has cryptic table names (`arinv`, `custmst`), status codes, voided and soft-deleted invoices, USD invoices that must be converted at the invoice-date rate, duplicate customers, and a fiscal year starting in July. About 4,900 invoices, 12,000 lines and 4,900 payments over two years.

1. **Semantic layer.** `lab/askfinance/semantic.py` defines what words mean here: revenue excludes voids and deletes and is converted to GHS; "this year" is the fiscal year; "now" is the data's as-of date. The model gets these rules, the schema and worked examples.
2. **Plan.** Claude answers through a tool schema: run SQL, ask a clarifying question, or refuse.
3. **Guard.** `sqlguard.py` parses the SQL (sqlglot) before it runs: one SELECT only, ERP tables only, no system functions, plus business checks (voids filtered, currency converted, no `CURRENT_DATE`). A failure goes back to the model with the reason; after two failed repairs it escalates to a stronger model.
4. **Run.** A read-only transaction with a 5-second timeout and a 200-row cap, so even a guard miss can't change data or hang the database.
5. **Answer.** A short answer that states its assumptions, with the SQL shown.

### Eval

`evals/askfinance_questions.py` holds 62 questions with gold SQL across nine groups: basics, time periods, currency, traps (voids, deletes, duplicates), joins, receivables, margin, questions that need clarifying, and ones that must be refused. Answers are scored by executing both queries and comparing values (0.5% tolerance), so different but correct SQL passes.

| Eval | Result |
| --- | --- |
| Gold SQL through the guard | 62/62 pass the guard and run |
| Live, Claude Haiku 5.5, first run | 55/62. Misses: added a `LIMIT` nobody asked for (2), couldn't map "25kg rice" to a product code, one result shaped as rows instead of columns, and 3 where the gold SQL itself contradicted the semantic layer |
| Live, after fixes | 62/62, 1 repair, median 3.3 s per question, $0.07 for the full set |

Fixes were a product catalog in the prompt, a rule against unasked limits, a definition for invoice "worth", corrected gold SQL, and a grader that accepts transposed results. A perfect score on a set the fixes were made against overstates real accuracy, so new questions keep being added. The latest live run is public at [`/v1/askfinance/eval`](https://lab-api-z3mx.onrender.com/v1/askfinance/eval) and shown on the demo page. Each question costs about $0.001.

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
