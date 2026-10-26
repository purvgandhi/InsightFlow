import pandas as pd

from insightflow.validation import validate_refunds, validate_transactions

NOW = pd.Timestamp("2026-10-01", tz="UTC")
BASE = {"customer_id": "C1", "merchant_id": "M1", "amount": "100.00", "fee_amount": "2.00",
        "currency": "INR", "status": "success", "payment_method": "card",
        "created_at": "2026-06-01T10:00:00+05:30"}


def txns(*rows):
    return pd.DataFrame([{**BASE, **r} for r in rows], dtype=str)


def codes(issues, record_id):
    return set(issues.loc[issues["record_id"] == record_id, "issue_code"])


def test_valid_rows_pass_and_are_normalised():
    clean, issues = validate_transactions(txns({"transaction_id": "T1", "status": " SUCCESS ",
                                                "payment_method": "UPI"}), now=NOW)
    assert issues.empty
    assert clean.loc[0, "status"] == "success" and clean.loc[0, "payment_method"] == "upi"
    assert clean.loc[0, "amount"] == 100.0


def test_each_defect_is_rejected_with_a_reason():
    clean, issues = validate_transactions(txns(
        {"transaction_id": "MISSING", "amount": ""},
        {"transaction_id": "NEG", "amount": "-5"},
        {"transaction_id": "TEXT", "amount": "12O0"},
        {"transaction_id": "BADTS", "created_at": "2026-13-45T10:00:00"},
        {"transaction_id": "FUTURE", "created_at": "2027-01-01T00:00:00+00:00"},
        {"transaction_id": "STATUS", "status": "completed"},
        {"transaction_id": "FEE", "fee_amount": "150"},
        {"transaction_id": "OK"},
    ), now=NOW)
    assert list(clean["transaction_id"]) == ["OK"]
    assert codes(issues, "MISSING") == {"MISSING_VALUE"}
    assert codes(issues, "NEG") == {"INVALID_AMOUNT"}
    assert codes(issues, "TEXT") == {"INVALID_AMOUNT"}
    assert codes(issues, "BADTS") == {"INVALID_TIMESTAMP"}
    assert codes(issues, "FUTURE") == {"FUTURE_TIMESTAMP"}
    assert codes(issues, "STATUS") == {"INVALID_STATUS"}
    assert codes(issues, "FEE") == {"INVALID_FEE"}


def test_exact_duplicate_keeps_one_copy_but_conflicting_duplicate_rejects_all():
    clean, issues = validate_transactions(txns(
        {"transaction_id": "DUP"}, {"transaction_id": "DUP"},
        {"transaction_id": "CONFLICT"}, {"transaction_id": "CONFLICT", "amount": "999.00"},
    ), now=NOW)
    assert list(clean["transaction_id"]) == ["DUP"]
    assert codes(issues, "DUP") == {"DUPLICATE_ROW"}
    assert (issues["issue_code"] == "DUPLICATE_ID_CONFLICT").sum() == 2


def test_refund_consistency_rules():
    parents = pd.DataFrame({
        "transaction_id": ["PAID", "FAILED"], "amount": [100.0, 50.0], "status": ["success", "failed"],
        "created_at": pd.to_datetime(["2026-06-01T10:00:00Z", "2026-06-01T10:00:00Z"]),
    })
    existing = pd.DataFrame({"refund_id": ["OLD"], "transaction_id": ["PAID"], "amount": [30.0]})
    refunds = pd.DataFrame([
        {"refund_id": "R1", "transaction_id": "PAID", "amount": "60", "refunded_at": "2026-06-02T10:00:00Z"},
        {"refund_id": "R2", "transaction_id": "PAID", "amount": "20", "refunded_at": "2026-06-03T10:00:00Z"},
        {"refund_id": "R3", "transaction_id": "NOPE", "amount": "5", "refunded_at": "2026-06-02T10:00:00Z"},
        {"refund_id": "R4", "transaction_id": "FAILED", "amount": "5", "refunded_at": "2026-06-02T10:00:00Z"},
        {"refund_id": "R5", "transaction_id": "PAID", "amount": "1", "refunded_at": "2026-05-01T10:00:00Z"},
    ], dtype=str)
    clean, issues = validate_refunds(refunds, parents, existing)
    assert codes(issues, "R5") == {"REFUND_BEFORE_PAYMENT"}
    assert codes(issues, "R3") == {"REFUND_UNKNOWN_TRANSACTION"}
    assert codes(issues, "R4") == {"REFUND_ON_UNSUCCESSFUL"}
    # 30 already refunded + 60 = 90 (ok), + 20 = 110 > 100 -> R2 rejected
    assert codes(issues, "R2") == {"REFUND_EXCEEDS_PAYMENT"}
    assert list(clean["refund_id"]) == ["R1"]
