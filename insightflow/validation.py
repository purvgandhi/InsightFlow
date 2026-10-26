"""Pandas data-quality checks for incoming transaction and refund files.

Each validator returns (clean_df, issues_df). A row with any problem is kept
out of the clean frame and gets one issue row per problem, so the reason a
record was rejected is always visible. These are *data errors* — a record we
cannot trust. Suspicious-but-valid records are handled in anomalies.py.
"""

import pandas as pd

VALID_STATUSES = {"success", "failed", "pending", "cancelled"}
VALID_METHODS = {"upi", "card", "netbanking", "wallet"}
TXN_REQUIRED = ["transaction_id", "customer_id", "merchant_id", "amount", "status",
                "payment_method", "created_at"]
REFUND_REQUIRED = ["refund_id", "transaction_id", "amount", "refunded_at"]
ISSUE_COLUMNS = ["row_number", "record_id", "issue_code", "message", "raw_record"]


def _issue(df: pd.DataFrame, mask: pd.Series, id_col: str, code: str, message) -> pd.DataFrame:
    """Build issue rows for every row where mask is True."""
    rows = df[mask.fillna(False).astype(bool)]
    if rows.empty:
        return pd.DataFrame(columns=ISSUE_COLUMNS)
    msgs = rows.apply(message, axis=1) if callable(message) else message
    return pd.DataFrame({
        "row_number": rows["_row"],
        "record_id": rows[id_col].fillna(""),
        "issue_code": code,
        "message": msgs,
        "raw_record": rows["_raw"],
    })


def _prepare(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    # Remember the original row (1-based, +1 for the CSV header) for reporting.
    df["_row"] = range(2, len(df) + 2)
    df["_raw"] = [{k: (None if pd.isna(v) else v) for k, v in rec.items()}
                  for rec in raw.to_dict("records")]
    for col in raw.columns:
        if df[col].dtype == object:
            df[col] = df[col].str.strip().replace("", pd.NA)
    return df


def _parse_timestamp(series: pd.Series) -> pd.Series:
    # Invalid dates (e.g. month 13) become NaT instead of raising.
    return pd.to_datetime(series, errors="coerce", utc=True, format="ISO8601")


def validate_transactions(raw: pd.DataFrame, now: pd.Timestamp | None = None):
    now = now or pd.Timestamp.now(tz="UTC")
    df = _prepare(raw)
    for col in TXN_REQUIRED:
        if col not in df.columns:
            df[col] = pd.NA
    if "currency" not in df.columns:
        df["currency"] = "INR"
    if "fee_amount" not in df.columns:
        df["fee_amount"] = "0"

    df["status"] = df["status"].fillna("").astype(str).str.lower().replace("", pd.NA)
    df["payment_method"] = df["payment_method"].fillna("").astype(str).str.lower().replace("", pd.NA)
    df["currency"] = df["currency"].fillna("INR").astype(str).str.upper()
    issues = []

    for col in TXN_REQUIRED:
        issues.append(_issue(df, df[col].isna(), "transaction_id", "MISSING_VALUE",
                             f"Required field '{col}' is empty"))

    amount = pd.to_numeric(df["amount"], errors="coerce")
    fee = pd.to_numeric(df["fee_amount"].fillna("0"), errors="coerce")
    has_amount = df["amount"].notna()
    issues.append(_issue(df, has_amount & amount.isna(), "transaction_id", "INVALID_AMOUNT",
                         lambda r: f"Amount '{r['amount']}' is not a number"))
    issues.append(_issue(df, amount.notna() & (amount <= 0), "transaction_id", "INVALID_AMOUNT",
                         lambda r: f"Amount {r['amount']} must be greater than zero"))
    issues.append(_issue(df, fee.isna() | (fee < 0) | ((amount > 0) & (fee > amount)),
                         "transaction_id", "INVALID_FEE", "Fee must be between 0 and the transaction amount"))

    created = _parse_timestamp(df["created_at"])
    has_ts = df["created_at"].notna()
    issues.append(_issue(df, has_ts & created.isna(), "transaction_id", "INVALID_TIMESTAMP",
                         lambda r: f"Timestamp '{r['created_at']}' cannot be parsed"))
    issues.append(_issue(df, created > now, "transaction_id", "FUTURE_TIMESTAMP",
                         lambda r: f"Timestamp {r['created_at']} is in the future"))

    issues.append(_issue(df, df["status"].notna() & ~df["status"].isin(VALID_STATUSES),
                         "transaction_id", "INVALID_STATUS",
                         lambda r: f"Status '{r['status']}' is not one of {sorted(VALID_STATUSES)}"))
    issues.append(_issue(df, df["payment_method"].notna() & ~df["payment_method"].isin(VALID_METHODS),
                         "transaction_id", "INVALID_PAYMENT_METHOD",
                         lambda r: f"Payment method '{r['payment_method']}' is not recognised"))
    issues.append(_issue(df, df["currency"] != "INR", "transaction_id", "INVALID_CURRENCY",
                         lambda r: f"Currency '{r['currency']}' is not supported (INR only)"))

    df["amount"], df["fee_amount"], df["created_at"] = amount, fee, created

    # Duplicate transaction IDs. An exact repeat of a row is harmless: keep one copy.
    # The same ID with *different* details is a conflict: we can't tell which
    # version is true, so every version is rejected rather than guessing.
    content = ["transaction_id", "customer_id", "merchant_id", "amount", "fee_amount",
               "status", "payment_method", "created_at"]
    exact_dup = df.duplicated(subset=content, keep="first")
    issues.append(_issue(df, exact_dup, "transaction_id", "DUPLICATE_ROW",
                         "Exact duplicate of an earlier row; the copy was skipped"))
    distinct = df[~exact_dup]
    conflict_ids = distinct.loc[distinct["transaction_id"].duplicated(keep=False)
                                & distinct["transaction_id"].notna(), "transaction_id"]
    conflict = ~exact_dup & df["transaction_id"].isin(conflict_ids)
    issues.append(_issue(df, conflict, "transaction_id", "DUPLICATE_ID_CONFLICT",
                         "Transaction ID appears more than once with different details"))

    issues_df = pd.concat(issues, ignore_index=True)
    bad_rows = set(issues_df["row_number"])
    clean = df[~df["_row"].isin(bad_rows)].drop(columns=["_row", "_raw"])
    return clean.reset_index(drop=True), issues_df.sort_values("row_number", ignore_index=True)


def validate_refunds(raw: pd.DataFrame, transactions: pd.DataFrame,
                     existing_refunds: pd.DataFrame | None = None):
    """Validate refunds against the known transactions.

    transactions needs: transaction_id, amount, status, created_at.
    existing_refunds (already in the database) needs: refund_id, transaction_id, amount.
    """
    df = _prepare(raw)
    issues = []
    for col in REFUND_REQUIRED:
        issues.append(_issue(df, df[col].isna(), "refund_id", "MISSING_VALUE",
                             f"Required field '{col}' is empty"))

    amount = pd.to_numeric(df["amount"], errors="coerce")
    issues.append(_issue(df, df["amount"].notna() & (amount.isna() | (amount <= 0)), "refund_id",
                         "INVALID_AMOUNT", lambda r: f"Refund amount '{r['amount']}' must be a positive number"))
    refunded_at = _parse_timestamp(df["refunded_at"])
    issues.append(_issue(df, df["refunded_at"].notna() & refunded_at.isna(), "refund_id",
                         "INVALID_TIMESTAMP", lambda r: f"Timestamp '{r['refunded_at']}' cannot be parsed"))
    df["amount"], df["refunded_at"] = amount, refunded_at

    exact_dup = df.duplicated(subset=["refund_id", "transaction_id", "amount", "refunded_at"], keep="first")
    issues.append(_issue(df, exact_dup, "refund_id", "DUPLICATE_ROW",
                         "Exact duplicate of an earlier row; the copy was skipped"))
    df = df.assign(_dup=exact_dup)

    txn = transactions.set_index("transaction_id")
    known = df["transaction_id"].isin(txn.index)
    issues.append(_issue(df, df["transaction_id"].notna() & ~known, "refund_id", "REFUND_UNKNOWN_TRANSACTION",
                         lambda r: f"Refund points to transaction {r['transaction_id']}, which does not exist"))

    parent_status = df["transaction_id"].map(txn["status"])
    parent_amount = df["transaction_id"].map(txn["amount"]).astype(float)
    parent_time = pd.to_datetime(df["transaction_id"].map(txn["created_at"]), utc=True)
    issues.append(_issue(df, known & (parent_status != "success"), "refund_id", "REFUND_ON_UNSUCCESSFUL",
                         lambda r: f"Original transaction status is '{parent_status[r.name]}', only successful payments can be refunded"))
    issues.append(_issue(df, known & refunded_at.notna() & (refunded_at < parent_time), "refund_id",
                         "REFUND_BEFORE_PAYMENT", "Refund is dated before the original payment"))

    # Over-refund: running total of refunds per transaction (oldest first, including
    # refunds already in the database) must not exceed the original amount.
    already = pd.Series(dtype=float)
    if existing_refunds is not None and len(existing_refunds):
        prior = existing_refunds[~existing_refunds["refund_id"].isin(df["refund_id"])]
        already = prior.groupby("transaction_id")["amount"].sum().astype(float)
    already_rejected = df["_row"].isin(set(pd.concat(issues)["row_number"]))
    candidates = df[~already_rejected].sort_values("refunded_at")
    running = candidates.groupby("transaction_id")["amount"].cumsum() \
        + candidates["transaction_id"].map(already).fillna(0)
    over = pd.Series(False, index=df.index)
    over[running.index] = running > parent_amount[running.index] + 0.005
    issues.append(_issue(df, over, "refund_id", "REFUND_EXCEEDS_PAYMENT",
                         lambda r: f"Refunds would total more than the original payment of ₹{parent_amount[r.name]:,.2f}"))

    issues_df = pd.concat(issues, ignore_index=True)
    bad_rows = set(issues_df["row_number"])
    clean = df[~df["_row"].isin(bad_rows)].drop(columns=["_row", "_raw", "_dup"])
    return clean.reset_index(drop=True), issues_df.sort_values("row_number", ignore_index=True)
