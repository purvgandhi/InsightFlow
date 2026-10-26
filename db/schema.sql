-- InsightFlow schema (PostgreSQL 14+). Safe to run repeatedly.

-- One row per payment attempt. transaction_id is the business key from the
-- source system, so re-importing the same file cannot create duplicates.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  TEXT PRIMARY KEY,
    customer_id     TEXT          NOT NULL,
    merchant_id     TEXT          NOT NULL,
    amount          NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    -- Processing fee the platform earns if the payment succeeds (our revenue).
    fee_amount      NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (fee_amount >= 0 AND fee_amount <= amount),
    currency        CHAR(3)       NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
    status          TEXT          NOT NULL CHECK (status IN ('success', 'failed', 'pending', 'cancelled')),
    payment_method  TEXT          NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL
);

-- A transaction can have zero or more (partial) refunds.
CREATE TABLE IF NOT EXISTS refunds (
    refund_id       TEXT PRIMARY KEY,
    transaction_id  TEXT          NOT NULL REFERENCES transactions (transaction_id),
    amount          NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    refunded_at     TIMESTAMPTZ   NOT NULL,
    reason          TEXT
);

-- Rows rejected by the Pandas validation step, with the reason.
CREATE TABLE IF NOT EXISTS data_quality_issues (
    id            BIGSERIAL PRIMARY KEY,
    source_file   TEXT        NOT NULL,
    row_number    INTEGER,
    record_id     TEXT,
    issue_code    TEXT        NOT NULL,
    message       TEXT        NOT NULL,
    raw_record    JSONB,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Valid transactions that look suspicious, with a human-readable explanation.
CREATE TABLE IF NOT EXISTS anomaly_flags (
    id              BIGSERIAL PRIMARY KEY,
    transaction_id  TEXT        NOT NULL REFERENCES transactions (transaction_id),
    rule_code       TEXT        NOT NULL,
    severity        TEXT        NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    explanation     TEXT        NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transaction_id, rule_code)
);

-- One row per import run, so the dashboard can say what data it is showing.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id             BIGSERIAL PRIMARY KEY,
    dataset_label  TEXT        NOT NULL,
    is_synthetic   BOOLEAN     NOT NULL,
    rows_received  INTEGER     NOT NULL,
    rows_loaded    INTEGER     NOT NULL,
    rows_rejected  INTEGER     NOT NULL,
    loaded_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Dashboard queries filter by date range, status and merchant.
CREATE INDEX IF NOT EXISTS idx_txn_created_at      ON transactions (created_at);
CREATE INDEX IF NOT EXISTS idx_txn_merchant_created ON transactions (merchant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_refunds_txn         ON refunds (transaction_id);
CREATE INDEX IF NOT EXISTS idx_flags_txn           ON anomaly_flags (transaction_id);
