-- Close Desk: invoices to the ledger, with three-way matching and fraud checks.

CREATE TABLE IF NOT EXISTS vendors (
    id                     serial PRIMARY KEY,
    name                   text NOT NULL UNIQUE,
    tin                    text,                      -- Ghana taxpayer ID
    email                  text,
    bank_name              text NOT NULL,
    bank_account           text NOT NULL,             -- the account on file, verified by phone
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendor_bank_history (
    id           serial PRIMARY KEY,
    vendor_id    integer NOT NULL REFERENCES vendors(id),
    bank_name    text NOT NULL,
    bank_account text NOT NULL,
    changed_at   timestamptz NOT NULL DEFAULT now(),
    verified_by  text
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id          serial PRIMARY KEY,
    po_number   text NOT NULL UNIQUE,
    vendor_id   integer NOT NULL REFERENCES vendors(id),
    currency    text NOT NULL DEFAULT 'GHS',
    issued_on   date NOT NULL,
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
);

CREATE TABLE IF NOT EXISTS po_lines (
    po_id       integer NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    line_no     integer NOT NULL,
    sku         text NOT NULL,
    description text NOT NULL,
    qty         numeric(12, 2) NOT NULL,
    unit_price  numeric(12, 2) NOT NULL,
    PRIMARY KEY (po_id, line_no)
);

CREATE TABLE IF NOT EXISTS delivery_notes (
    id           serial PRIMARY KEY,
    dn_number    text NOT NULL UNIQUE,
    po_id        integer NOT NULL REFERENCES purchase_orders(id),
    received_on  date NOT NULL
);

CREATE TABLE IF NOT EXISTS dn_lines (
    dn_id        integer NOT NULL REFERENCES delivery_notes(id) ON DELETE CASCADE,
    sku          text NOT NULL,
    qty_received numeric(12, 2) NOT NULL,
    PRIMARY KEY (dn_id, sku)
);

CREATE TABLE IF NOT EXISTS invoices (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid REFERENCES runs(id) ON DELETE SET NULL,
    vendor_id           integer REFERENCES vendors(id),
    vendor_name_raw     text,
    invoice_number      text,
    invoice_number_norm text,
    invoice_date        date,
    po_number           text,
    currency            text,
    subtotal            numeric(12, 2),
    tax                 numeric(12, 2),
    total               numeric(12, 2),
    bank_name           text,
    bank_account        text,
    file_sha256         text,
    status              text NOT NULL CHECK (status IN ('posted', 'pending_approval', 'held', 'rejected')),
    extracted           jsonb NOT NULL DEFAULT '{}'::jsonb,
    confidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoices_vendor_norm_idx ON invoices (vendor_id, invoice_number_norm);
CREATE INDEX IF NOT EXISTS invoices_sha_idx ON invoices (file_sha256);

CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_id  uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no     integer NOT NULL,
    sku         text,
    description text,
    qty         numeric(12, 2),
    unit_price  numeric(12, 2),
    amount      numeric(12, 2),
    PRIMARY KEY (invoice_id, line_no)
);

CREATE TABLE IF NOT EXISTS invoice_checks (
    invoice_id  uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    check_name  text NOT NULL,
    result      text NOT NULL CHECK (result IN ('pass', 'warn', 'fail')),
    severity    text NOT NULL CHECK (severity IN ('info', 'review', 'fraud')),
    detail      text,
    PRIMARY KEY (invoice_id, check_name)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id          bigserial PRIMARY KEY,
    invoice_id  uuid NOT NULL REFERENCES invoices(id),
    account     text NOT NULL,
    debit       numeric(12, 2) NOT NULL DEFAULT 0,
    credit      numeric(12, 2) NOT NULL DEFAULT 0,
    memo        text,
    posted_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    id           bigserial PRIMARY KEY,
    invoice_id   uuid NOT NULL REFERENCES invoices(id),
    requested_at timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    decision     text CHECK (decision IN ('approve', 'reject')),
    decided_by   text,
    reason       text
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial PRIMARY KEY,
    entity     text NOT NULL,
    entity_id  text NOT NULL,
    action     text NOT NULL,
    actor      text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now(),
    data       jsonb NOT NULL DEFAULT '{}'::jsonb
);
