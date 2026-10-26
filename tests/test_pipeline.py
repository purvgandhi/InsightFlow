import pandas as pd

from insightflow.pipeline import run_pipeline

TXNS = """transaction_id,customer_id,merchant_id,amount,fee_amount,currency,status,payment_method,created_at
T1,C1,M1,100.00,2.00,INR,success,card,2026-06-01T10:00:00+05:30
T2,C2,M1,50.00,0.00,INR,failed,upi,2026-06-01T11:00:00+05:30
T2,C2,M1,50.00,0.00,INR,failed,upi,2026-06-01T11:00:00+05:30
T3,C3,M1,-10.00,0.00,INR,success,upi,2026-06-01T12:00:00+05:30
"""
REFUNDS = """refund_id,transaction_id,amount,refunded_at,reason
R1,T1,40.00,2026-06-02T10:00:00+05:30,customer_request
R2,T2,10.00,2026-06-02T10:00:00+05:30,customer_request
"""


def test_import_is_validated_and_idempotent(db_conn, tmp_path):
    (tmp_path / "t.csv").write_text(TXNS)
    (tmp_path / "r.csv").write_text(REFUNDS)
    now = pd.Timestamp("2026-10-01", tz="UTC")

    first = run_pipeline(db_conn, tmp_path / "t.csv", tmp_path / "r.csv", "test", True, reset=True, now=now)
    assert first["transactions_inserted"] == 2          # T1, T2 (duplicate copy skipped, T3 rejected)
    assert first["refunds_inserted"] == 1               # R2 rejected: T2 failed
    assert first["issues_by_code"] == {"DUPLICATE_ROW": 1, "INVALID_AMOUNT": 1, "REFUND_ON_UNSUCCESSFUL": 1}

    second = run_pipeline(db_conn, tmp_path / "t.csv", tmp_path / "r.csv", "test", True, now=now)
    assert second["transactions_inserted"] == 0 and second["transactions_already_present"] == 2
    assert second["refunds_inserted"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    assert db_conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 1
    # Issues are replaced per file, not appended, on re-import.
    assert db_conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0] == 3
