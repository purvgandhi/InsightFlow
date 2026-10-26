import pandas as pd

from insightflow.anomalies import detect_anomalies


def _txn(tid, ts, amount=100.0, status="success", customer="C1", merchant="M1"):
    return {"transaction_id": tid, "customer_id": customer, "merchant_id": merchant,
            "amount": amount, "status": status, "created_at": pd.Timestamp(ts, tz="UTC")}


def flags_for(txns, refunds=None):
    refunds = refunds if refunds is not None else pd.DataFrame(columns=["transaction_id", "amount", "refunded_at"])
    flags = detect_anomalies(pd.DataFrame(txns), refunds)
    return {(r.transaction_id, r.rule_code) for r in flags.itertuples()}


def test_possible_double_charge_needs_same_amount_within_window():
    flags = flags_for([
        _txn("A", "2026-06-01 10:00:00"), _txn("B", "2026-06-01 10:01:00"),   # 60s later -> flag B
        _txn("C", "2026-06-01 12:00:00"), _txn("D", "2026-06-01 12:30:00"),   # 30 min later -> no flag
        _txn("E", "2026-06-01 14:00:00"), _txn("F", "2026-06-01 14:00:30", amount=101.0),  # different amount
    ])
    assert flags == {("B", "POSSIBLE_DOUBLE_CHARGE")}


def test_failed_attempt_burst():
    burst = [_txn(f"F{i}", f"2026-06-01 02:0{i}:00", status="failed", customer="C9") for i in range(4)]
    spread = [_txn(f"S{i}", f"2026-06-0{i + 1} 02:00:00", status="failed", customer="C8") for i in range(4)]
    flags = flags_for(burst + spread)
    assert flags == {("F2", "FAILED_ATTEMPT_BURST"), ("F3", "FAILED_ATTEMPT_BURST")}


def test_high_value_relative_to_merchant_median():
    normal = [_txn(f"N{i}", f"2026-06-01 10:{i:02d}:00", amount=100.0, customer=f"C{i}") for i in range(40)]
    big = _txn("BIG", "2026-06-02 10:00:00", amount=900.0, customer="CX")
    assert flags_for(normal + [big]) == {("BIG", "HIGH_VALUE")}


def test_quick_full_refund_only_when_fully_refunded_fast():
    txns = [_txn("Q", "2026-06-01 10:00:00"), _txn("P", "2026-06-01 11:00:00", customer="C2"),
            _txn("L", "2026-06-01 12:00:00", customer="C3")]
    refunds = pd.DataFrame({
        "transaction_id": ["Q", "P", "L"], "amount": [100.0, 40.0, 100.0],
        "refunded_at": pd.to_datetime(["2026-06-01 10:10:00", "2026-06-01 11:05:00", "2026-06-03 12:00:00"], utc=True),
    })
    assert flags_for(txns, refunds) == {("Q", "QUICK_FULL_REFUND")}
