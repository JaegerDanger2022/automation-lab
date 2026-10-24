-- A small legacy-style ERP for "Northwind Ghana Ltd", a distributor with four branches.
-- Deliberately awkward, like the real thing: cryptic names, status codes, soft deletes,
-- mixed currencies, a July fiscal year, and duplicate customers.
-- The Ask Finance agent reads it through a read-only connection and a semantic layer.

CREATE SCHEMA IF NOT EXISTS erp;

CREATE TABLE IF NOT EXISTS erp.brnch (
    br_cd   text PRIMARY KEY,          -- ACC, KSI, TKD, TML
    br_nm   text NOT NULL,
    rgn     text NOT NULL              -- region name
);

CREATE TABLE IF NOT EXISTS erp.slsrep (
    rep_cd  text PRIMARY KEY,
    rep_nm  text NOT NULL,
    br_cd   text NOT NULL REFERENCES erp.brnch(br_cd),
    actv    char(1) NOT NULL DEFAULT 'Y'
);

CREATE TABLE IF NOT EXISTS erp.custmst (
    cust_cd   text PRIMARY KEY,        -- C00001
    cust_nm   text NOT NULL,           -- free text, so the same business can appear twice
    seg_cd    text NOT NULL,           -- RT retail, WS wholesale, GV government, NG NGO
    br_cd     text NOT NULL REFERENCES erp.brnch(br_cd),
    rep_cd    text REFERENCES erp.slsrep(rep_cd),
    crdt_lmt  numeric(14, 2),          -- credit limit in GHS
    actv_flg  char(1) NOT NULL DEFAULT 'Y',
    crt_dt    date NOT NULL
);

CREATE TABLE IF NOT EXISTS erp.catmst (
    cat_cd    text PRIMARY KEY,
    cat_desc  text NOT NULL
);

CREATE TABLE IF NOT EXISTS erp.itmmst (
    itm_cd    text PRIMARY KEY,
    itm_desc  text NOT NULL,
    cat_cd    text NOT NULL REFERENCES erp.catmst(cat_cd),
    uom       text NOT NULL,
    std_cost  numeric(14, 2) NOT NULL  -- unit cost in GHS
);

CREATE TABLE IF NOT EXISTS erp.arinv (
    inv_no    text PRIMARY KEY,        -- ACC-24-000123
    cust_cd   text NOT NULL REFERENCES erp.custmst(cust_cd),
    br_cd     text NOT NULL REFERENCES erp.brnch(br_cd),
    inv_dt    date NOT NULL,
    due_dt    date NOT NULL,
    curr_cd   text NOT NULL,           -- GHS or USD
    fx_rt     numeric(10, 4) NOT NULL, -- rate to GHS on the invoice date (1 for GHS)
    net_amt   numeric(14, 2) NOT NULL, -- before tax, invoice currency
    tax_amt   numeric(14, 2) NOT NULL, -- invoice currency
    sts       char(1) NOT NULL,        -- O open, P paid, V void
    del_flg   smallint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS arinv_dt_idx ON erp.arinv (inv_dt);
CREATE INDEX IF NOT EXISTS arinv_cust_idx ON erp.arinv (cust_cd);

CREATE TABLE IF NOT EXISTS erp.arinvln (
    inv_no    text NOT NULL REFERENCES erp.arinv(inv_no),
    ln_no     int NOT NULL,
    itm_cd    text NOT NULL REFERENCES erp.itmmst(itm_cd),
    qty       numeric(12, 2) NOT NULL,
    unit_prc  numeric(14, 2) NOT NULL, -- invoice currency
    disc_pct  numeric(5, 2) NOT NULL DEFAULT 0,
    PRIMARY KEY (inv_no, ln_no)
);

CREATE TABLE IF NOT EXISTS erp.arpmt (
    pmt_id    text PRIMARY KEY,
    cust_cd   text NOT NULL REFERENCES erp.custmst(cust_cd),
    pmt_dt    date NOT NULL,
    curr_cd   text NOT NULL,
    fx_rt     numeric(10, 4) NOT NULL,
    amt       numeric(14, 2) NOT NULL, -- payment currency
    mthd      text NOT NULL,           -- MOMO, BANK, CHQ, CASH
    ref       text
);

CREATE TABLE IF NOT EXISTS erp.aralloc (
    pmt_id     text NOT NULL REFERENCES erp.arpmt(pmt_id),
    inv_no     text NOT NULL REFERENCES erp.arinv(inv_no),
    alloc_amt  numeric(14, 2) NOT NULL, -- invoice currency
    PRIMARY KEY (pmt_id, inv_no)
);
