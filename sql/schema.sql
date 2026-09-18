-- ============================================================================
-- BankScope PostgreSQL schema
--
-- Designed around the ACTUAL confirmed shape of parse_statement_page() /
-- parse_schedule() output (src/extraction.py), not a generic textbook
-- design -- see docs/progress.md for the extraction findings this reflects.
--
-- Layered design, deliberately not "raw PDF straight into named columns":
--   1. raw_line_items   -- everything extracted, verbatim, one row per line
--   2. financial_facts  -- normalized to canonical keys, ready for ratios
--   3. computed_ratios  -- final ratio output, with disclosed values
--                          alongside computed ones as a built-in accuracy
--                          check (see README's "computed vs disclosed" note)
--
-- Currently populated by the pipeline: banks, raw_line_items (BS/P&L/CF +
-- Schedule 3). financial_facts and computed_ratios are defined now so the
-- load/ratio-calculation steps have a confirmed target shape to write to,
-- but are not populated until src/load_to_db.py and the ratio SQL views
-- are implemented (next steps).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- banks: one row per bank in scope. bank_code matches the folder name under
-- data/raw_reports/ (see src/config.py's BANKS dict) so loading code can key
-- off the same identifier used on disk, no separate mapping needed.
-- ----------------------------------------------------------------------------
CREATE TABLE banks (
    bank_code   VARCHAR(30)  PRIMARY KEY,
    bank_name   VARCHAR(100) NOT NULL,
    bank_type   VARCHAR(10)  NOT NULL CHECK (bank_type IN ('private', 'psu')),
    UNIQUE (bank_name)
);

INSERT INTO banks (bank_code, bank_name, bank_type) VALUES
    ('hdfc_bank',      'HDFC Bank',              'private'),
    ('icici_bank',     'ICICI Bank',             'private'),
    ('sbi',            'State Bank of India',    'psu'),
    ('axis_bank',      'Axis Bank',              'private'),
    ('kotak_mahindra', 'Kotak Mahindra Bank',    'private');


-- ----------------------------------------------------------------------------
-- raw_line_items: the landing zone. One row per line item exactly as
-- parse_statement_page()/parse_schedule() extracted it -- label text
-- included verbatim, wrapped-label noise and all. This is the audit trail:
-- if a computed ratio ever looks wrong, this table is where you trace it
-- back to the exact extracted row (and from there, the exact PDF page) it
-- came from.
--
-- statement_type covers both the 3 main statements and schedules:
--   'balance_sheet' | 'profit_loss' | 'cash_flow' | 'schedule_<N>'
-- (e.g. 'schedule_3' for Deposits) -- kept as a single text field rather
-- than a separate schedule_number column, since the main statements don't
-- have a schedule number of their own but do need a slot in the same field.
--
-- consolidated distinguishes standalone vs consolidated filings, matching
-- ReportMap's is_consolidated flag from the locator. The project uses
-- standalone throughout (see README's "why standalone" note), but
-- consolidated is captured too since the locator already finds it --
-- discarding it at extraction time would throw away data for no reason.
-- ----------------------------------------------------------------------------
CREATE TABLE raw_line_items (
    id                  SERIAL PRIMARY KEY,
    bank_code           VARCHAR(30)  NOT NULL REFERENCES banks(bank_code),
    fiscal_year         SMALLINT     NOT NULL,  -- year the REPORT covers, e.g. 2022 for FY2021-22
    statement_type      VARCHAR(20)  NOT NULL,
    consolidated        BOOLEAN      NOT NULL DEFAULT FALSE,
    section             VARCHAR(60),             -- e.g. 'CAPITAL AND LIABILITIES', 'ASSETS'; NULL for schedule rows
    schedule_ref        SMALLINT,                -- the inline schedule # a BS/P&L line points to, e.g. Capital -> 1; NULL if none
    label               TEXT         NOT NULL,   -- verbatim extracted label, wrapped-line noise included
    current_year_value  NUMERIC(20, 2),          -- NULL means the source used "-" (nil/not applicable), not zero
    prior_year_value    NUMERIC(20, 2),
    source_page         SMALLINT,                -- PDF page number the value was extracted from, for traceability
    extracted_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_raw_line_items_lookup
    ON raw_line_items (bank_code, fiscal_year, statement_type, consolidated);

-- Deliberately NOT a unique constraint on (bank_code, fiscal_year,
-- statement_type, label): labels are not unique within a page (HDFC's
-- Deposits schedule has "Total" appear 3 times -- see docs/progress.md),
-- so line order / id is the only reliable way to distinguish rows with a
-- repeated label. Downstream normalization (financial_facts) is where
-- disambiguation actually happens, using position, not a DB constraint.


-- ----------------------------------------------------------------------------
-- financial_facts: normalized layer. One row per (bank, year, canonical
-- key) -- e.g. ('icici_bank', 2022, 'interest_earned', 258...). This is
-- what the ratio-calculation layer actually reads from; it should never
-- need to parse a raw label string.
--
-- canonical_key values are deliberately scoped to exactly what the 7
-- planned ratios need (see src/ratios.py for the formulas), not an
-- exhaustive re-encoding of every line item in every statement:
--
--   Balance Sheet:  total_assets, total_deposits, total_advances,
--                    demand_deposits_from_others, savings_deposits
--                    (CASA = demand_deposits_from_others + savings_deposits
--                    -- see the positional-disambiguation note in
--                    docs/progress.md for why "from others" needs the
--                    FIRST occurrence in the Deposits schedule specifically)
--   P&L:             interest_earned, interest_expended, other_income,
--                    operating_expenses, net_profit
--   Disclosed         disclosed_gross_npa, disclosed_net_npa, disclosed_crar
--   (bank's own        -- not yet parsed by the pipeline (these live in
--   reported figures,   MD&A tables / Schedule notes, not yet built --
--   not computed)        see docs/progress.md "Next up"), included here
--                        so the schema doesn't need revisiting once they are
--
-- demand_deposits_from_others and savings_deposits populate the CASA
-- inputs; total_advances/total_deposits also support the YoY growth ratio
-- using prior_year_value already captured one layer down, or by comparing
-- consecutive fiscal_year rows here.
-- ----------------------------------------------------------------------------
CREATE TABLE financial_facts (
    bank_code       VARCHAR(30)  NOT NULL REFERENCES banks(bank_code),
    fiscal_year     SMALLINT     NOT NULL,
    canonical_key   VARCHAR(50)  NOT NULL,
    value           NUMERIC(20, 2),
    source_line_item_id INTEGER  REFERENCES raw_line_items(id),  -- traceability back to the exact raw row
    PRIMARY KEY (bank_code, fiscal_year, canonical_key)
);


-- ----------------------------------------------------------------------------
-- computed_ratios: final output. Stores BOTH the value computed from raw
-- line items AND the bank's own disclosed value (where available) side by
-- side, as a built-in accuracy check -- see README's "Computed vs.
-- disclosed ratios" note. disclosed_value is nullable since not every
-- ratio has a directly-disclosed counterpart to check against (e.g. Cost-
-- to-Income isn't typically disclosed as a single number the way CRAR or
-- NPA% are).
-- ----------------------------------------------------------------------------
CREATE TABLE computed_ratios (
    bank_code       VARCHAR(30)  NOT NULL REFERENCES banks(bank_code),
    fiscal_year     SMALLINT     NOT NULL,
    ratio_name      VARCHAR(30)  NOT NULL,  -- 'nim' | 'casa_ratio' | 'gross_npa_pct' | 'net_npa_pct'
                                             -- 'crar' | 'roa' | 'roe' | 'cost_to_income'
                                             -- 'advances_yoy_growth' | 'deposits_yoy_growth'
    computed_value  NUMERIC(10, 4),
    disclosed_value NUMERIC(10, 4),
    computed_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (bank_code, fiscal_year, ratio_name)
);