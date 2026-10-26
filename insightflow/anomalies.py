"""Explainable anomaly rules over *valid* transactions (Pandas).

Unlike validation errors, these records are well-formed and are counted in
the KPIs; they are flagged for a human to review. Each rule is a simple,
tunable threshold and every flag carries a sentence explaining why it fired.
"""

import pandas as pd

HIGH_VALUE_MULTIPLIER = 8       # amount >= 8x the merchant's median successful amount
HIGH_VALUE_MIN_HISTORY = 30     # only judge merchants with enough history
DOUBLE_CHARGE_WINDOW_S = 120    # same customer + merchant + amount within 2 minutes
FAILED_BURST_COUNT = 3          # >= 3 failed attempts by one customer ...
FAILED_BURST_WINDOW_MIN = 10    # ... within 10 minutes
QUICK_REFUND_WINDOW_MIN = 60    # full refund within 1 hour of payment

FLAG_COLUMNS = ["transaction_id", "rule_code", "severity", "explanation"]


def _inr(x: float) -> str:
    return f"₹{x:,.2f}"


def high_value(txns: pd.DataFrame) -> pd.DataFrame:
    ok = txns[txns["status"] == "success"]
    stats = ok.groupby("merchant_id")["amount"].agg(["median", "count"])
    df = ok.join(stats, on="merchant_id")
    hit = df[(df["count"] >= HIGH_VALUE_MIN_HISTORY) & (df["amount"] >= HIGH_VALUE_MULTIPLIER * df["median"])]
    return pd.DataFrame({
        "transaction_id": hit["transaction_id"],
        "rule_code": "HIGH_VALUE",
        "severity": "medium",
        "explanation": [f"{_inr(a)} is {a / m:.1f}x the merchant's median payment of {_inr(m)}"
                        for a, m in zip(hit["amount"], hit["median"])],
    })


def possible_double_charge(txns: pd.DataFrame) -> pd.DataFrame:
    ok = txns[txns["status"] == "success"].sort_values("created_at")
    key = ["customer_id", "merchant_id", "amount"]
    prev_time = ok.groupby(key)["created_at"].shift(1)
    prev_id = ok.groupby(key)["transaction_id"].shift(1)
    gap = (ok["created_at"] - prev_time).dt.total_seconds()
    hit = ok[gap <= DOUBLE_CHARGE_WINDOW_S]
    return pd.DataFrame({
        "transaction_id": hit["transaction_id"],
        "rule_code": "POSSIBLE_DOUBLE_CHARGE",
        "severity": "high",
        "explanation": [f"Same customer, merchant and amount ({_inr(a)}) as {p}, {int(g)}s earlier"
                        for a, p, g in zip(hit["amount"], prev_id[hit.index], gap[hit.index])],
    })


def failed_attempt_burst(txns: pd.DataFrame) -> pd.DataFrame:
    # Sorted by (customer, time) so the grouped rolling result lines up row-for-row.
    failed = txns[txns["status"] == "failed"].sort_values(["customer_id", "created_at"])
    if failed.empty:
        return pd.DataFrame(columns=FLAG_COLUMNS)
    window = f"{FAILED_BURST_WINDOW_MIN}min"
    counts = failed.set_index("created_at").groupby("customer_id")["amount"].rolling(window).count()
    failed = failed.assign(burst=counts.values)
    hit = failed[failed["burst"] >= FAILED_BURST_COUNT]
    return pd.DataFrame({
        "transaction_id": hit["transaction_id"].values,
        "rule_code": "FAILED_ATTEMPT_BURST",
        "severity": "high",
        "explanation": [f"Customer {c} had {int(n)} failed payments within {FAILED_BURST_WINDOW_MIN} minutes "
                        "(possible card testing)" for c, n in zip(hit["customer_id"], hit["burst"])],
    })


def quick_full_refund(txns: pd.DataFrame, refunds: pd.DataFrame) -> pd.DataFrame:
    if refunds.empty:
        return pd.DataFrame(columns=FLAG_COLUMNS)
    per_txn = refunds.groupby("transaction_id").agg(refunded=("amount", "sum"), last=("refunded_at", "max"))
    df = txns.join(per_txn, on="transaction_id", how="inner")
    minutes = (df["last"] - df["created_at"]).dt.total_seconds() / 60
    hit = df[(df["refunded"] >= df["amount"]) & (minutes <= QUICK_REFUND_WINDOW_MIN)]
    return pd.DataFrame({
        "transaction_id": hit["transaction_id"],
        "rule_code": "QUICK_FULL_REFUND",
        "severity": "low",
        "explanation": [f"Fully refunded ({_inr(a)}) only {int(m)} minutes after payment"
                        for a, m in zip(hit["amount"], minutes[hit.index])],
    })


def detect_anomalies(txns: pd.DataFrame, refunds: pd.DataFrame) -> pd.DataFrame:
    """txns: transaction_id, customer_id, merchant_id, amount, status, created_at.
    refunds: transaction_id, amount, refunded_at."""
    txns = txns.assign(amount=txns["amount"].astype(float),
                       created_at=pd.to_datetime(txns["created_at"], utc=True))
    refunds = refunds.assign(amount=refunds["amount"].astype(float),
                             refunded_at=pd.to_datetime(refunds["refunded_at"], utc=True))
    flags = [high_value(txns), possible_double_charge(txns), failed_attempt_burst(txns),
             quick_full_refund(txns, refunds)]
    flags = [f for f in flags if not f.empty]
    if not flags:
        return pd.DataFrame(columns=FLAG_COLUMNS)
    return pd.concat(flags, ignore_index=True)[FLAG_COLUMNS]
