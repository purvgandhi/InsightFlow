"""Load CSV files into PostgreSQL: read -> validate (Pandas) -> insert -> flag anomalies.

The whole run is one database transaction, so a failure leaves the database
unchanged. Inserts use ON CONFLICT DO NOTHING on the business keys, so
re-running the same import is idempotent (no duplicate transactions).
"""

import io
import json
from pathlib import Path

import pandas as pd
import psycopg

from .anomalies import detect_anomalies
from .validation import validate_refunds, validate_transactions

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
TXN_COLS = ["transaction_id", "customer_id", "merchant_id", "amount", "fee_amount", "currency",
            "status", "payment_method", "created_at"]
REFUND_COLS = ["refund_id", "transaction_id", "amount", "refunded_at", "reason"]


def read_csv(source) -> pd.DataFrame:
    # Read everything as text so validation (not pandas' type guessing) decides what is valid.
    if isinstance(source, pd.DataFrame):
        return source
    if isinstance(source, (str, bytes)):
        content = source if isinstance(source, str) else source.decode("utf-8", errors="replace")
        if "\n" in content or ("," in content and not Path(content).exists()):
            return pd.read_csv(io.StringIO(content), dtype=str, keep_default_na=False, na_values=[""])
    return pd.read_csv(source, dtype=str, keep_default_na=False, na_values=[""])


def _copy_insert(cur, table: str, cols: list[str], df: pd.DataFrame, conflict_key: str) -> int:
    """Bulk-load via COPY into a temp table, then insert only rows whose key is new."""
    cur.execute(f"CREATE TEMP TABLE stage_{table} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy(f"COPY stage_{table} ({', '.join(cols)}) FROM STDIN") as copy:
        for row in df[cols].itertuples(index=False, name=None):
            copy.write_row([None if pd.isna(v) else v for v in row])
    cur.execute(f"""INSERT INTO {table} ({', '.join(cols)})
                    SELECT {', '.join(cols)} FROM stage_{table}
                    ON CONFLICT ({conflict_key}) DO NOTHING""")
    return cur.rowcount


def _frame(cur, sql: str) -> pd.DataFrame:
    cur.execute(sql)
    return pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])


def _save_issues(cur, source_file: str, issues: pd.DataFrame) -> None:
    cur.execute("DELETE FROM data_quality_issues WHERE source_file = %s", (source_file,))
    cur.executemany(
        """INSERT INTO data_quality_issues (source_file, row_number, record_id, issue_code, message, raw_record)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        [(source_file, int(r.row_number), r.record_id, r.issue_code, r.message,
          json.dumps(r.raw_record, default=str)) for r in issues.itertuples()])


def run_pipeline(conn: psycopg.Connection, transactions_csv, refunds_csv=None, dataset_label: str = "Imported dataset",
                 is_synthetic: bool = False, reset: bool = False, now: pd.Timestamp | None = None) -> dict:
    txn_raw = read_csv(transactions_csv)
    txn_name = "transactions.csv"
    if isinstance(transactions_csv, (str, Path)) and Path(str(transactions_csv)).exists():
        txn_name = Path(str(transactions_csv)).name

    if refunds_csv is not None:
        refund_raw = read_csv(refunds_csv)
        refund_name = Path(str(refunds_csv)).name if isinstance(refunds_csv, (str, Path)) and Path(str(refunds_csv)).exists() else "refunds.csv"
    else:
        refund_raw = pd.DataFrame(columns=REFUND_COLS)
        refund_name = "refunds.csv"

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
        if reset:
            cur.execute("TRUNCATE anomaly_flags, refunds, transactions, data_quality_issues, ingestion_runs")

        # 1. Transactions
        txns, txn_issues = validate_transactions(txn_raw, now=now)
        inserted_txns = _copy_insert(cur, "transactions", TXN_COLS, txns, "transaction_id")
        _save_issues(cur, txn_name, txn_issues)

        # 2. Refunds, checked against everything now in the database
        all_txns = _frame(cur, "SELECT transaction_id, customer_id, merchant_id, amount, status, created_at FROM transactions")
        existing_refunds = _frame(cur, "SELECT refund_id, transaction_id, amount, refunded_at FROM refunds")
        refunds, refund_issues = validate_refunds(refund_raw, all_txns, existing_refunds)
        inserted_refunds = _copy_insert(cur, "refunds", REFUND_COLS, refunds, "refund_id")
        _save_issues(cur, refund_name, refund_issues)

        # 3. Anomaly flags are recomputed over the full history (rules compare against medians etc.)
        all_refunds = _frame(cur, "SELECT transaction_id, amount, refunded_at FROM refunds")
        flags = detect_anomalies(all_txns, all_refunds)
        cur.execute("DELETE FROM anomaly_flags")
        cur.executemany(
            "INSERT INTO anomaly_flags (transaction_id, rule_code, severity, explanation) VALUES (%s, %s, %s, %s)",
            list(flags.itertuples(index=False, name=None)))

        rows_received = len(txn_raw) + len(refund_raw)
        rejected_rows = txn_issues["row_number"].nunique() + refund_issues["row_number"].nunique()
        cur.execute(
            """INSERT INTO ingestion_runs (dataset_label, is_synthetic, rows_received, rows_loaded, rows_rejected)
               VALUES (%s, %s, %s, %s, %s)""",
            (dataset_label, is_synthetic, rows_received, inserted_txns + inserted_refunds, rejected_rows))

    return {
        "transactions_in_file": len(txn_raw), "transactions_valid": len(txns),
        "transactions_inserted": inserted_txns, "transactions_already_present": len(txns) - inserted_txns,
        "refunds_in_file": len(refund_raw), "refunds_valid": len(refunds), "refunds_inserted": inserted_refunds,
        "issues": int(len(txn_issues) + len(refund_issues)),
        "issues_by_code": pd.concat([txn_issues, refund_issues])["issue_code"].value_counts().to_dict(),
        "anomaly_flags": len(flags),
        "flags_by_rule": flags["rule_code"].value_counts().to_dict(),
    }
