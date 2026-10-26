"""SQL behind every number on the dashboard.

KPI definitions (see README "Metric definitions"):
  total_transactions  = every payment attempt in the date range (all statuses)
  gtv                 = SUM(amount) of SUCCESSFUL transactions (before refunds)
  refund_amount       = SUM(refunds.amount) for successful transactions in the range
                        (refunds are attributed to the original payment's date)
  net_transaction_val = gtv - refund_amount
  fee_revenue         = SUM(fee_amount) of successful transactions
  net_revenue         = fee_revenue - fees reversed on refunds
                        (fee reversed = fee * refunded / amount, i.e. pro-rata)
  refund_rate         = refund_amount / gtv
  aov                 = gtv / number of successful transactions
  success_rate        = successful / (successful + failed)  (pending/cancelled excluded)

Dates are interpreted in REPORTING_TZ (default Asia/Kolkata). Every value that
comes from the user is passed as a bound parameter, never formatted into SQL.
"""

import os
from datetime import date, timedelta
from decimal import Decimal

from .db import fetch_all, fetch_one

TZ = os.environ.get("REPORTING_TZ", "Asia/Kolkata")

# Shared filter. The end date is inclusive, so we compare against the start of the next day.
FILTER = """
    t.created_at >= (%(start)s::date)::timestamp AT TIME ZONE %(tz)s
    AND t.created_at < (%(end)s::date + 1)::timestamp AT TIME ZONE %(tz)s
    AND (%(merchant)s::text IS NULL OR t.merchant_id = %(merchant)s)
    AND (%(method)s::text IS NULL OR t.payment_method = %(method)s)
"""


def _params(start: date, end: date, merchant: str | None, method: str | None, **extra) -> dict:
    return {"start": start, "end": end, "merchant": merchant, "method": method, "tz": TZ, **extra}


def _div(a, b) -> Decimal | None:
    return None if not b else Decimal(a) / Decimal(b)


def get_meta() -> dict:
    row = fetch_one("""
        SELECT (MIN(created_at) AT TIME ZONE %(tz)s)::date AS min_date,
               (MAX(created_at) AT TIME ZONE %(tz)s)::date AS max_date,
               COUNT(*) AS transactions
        FROM transactions""", {"tz": TZ})
    merchants = [r["merchant_id"] for r in fetch_all(
        "SELECT DISTINCT merchant_id FROM transactions ORDER BY 1")]
    methods = [r["payment_method"] for r in fetch_all(
        "SELECT DISTINCT payment_method FROM transactions ORDER BY 1")]
    run = fetch_one("SELECT dataset_label, is_synthetic, loaded_at FROM ingestion_runs ORDER BY id DESC LIMIT 1")
    return {**row, "merchants": merchants, "payment_methods": methods, "reporting_tz": TZ, "dataset": run}


KPI_SQL = f"""
WITH f AS (
    SELECT t.* FROM transactions t WHERE {FILTER}
),
r AS (  -- total refunded per successful transaction (a payment can have several partial refunds)
    SELECT rf.transaction_id, SUM(rf.amount) AS refunded
    FROM refunds rf JOIN f ON f.transaction_id = rf.transaction_id AND f.status = 'success'
    GROUP BY rf.transaction_id
)
SELECT
    COUNT(*)                                                    AS total_transactions,
    COUNT(*) FILTER (WHERE f.status = 'success')                AS successful,
    COUNT(*) FILTER (WHERE f.status = 'failed')                 AS failed,
    COUNT(*) FILTER (WHERE f.status = 'pending')                AS pending,
    COUNT(*) FILTER (WHERE f.status = 'cancelled')              AS cancelled,
    COALESCE(SUM(f.amount), 0)                                  AS attempted_value,
    COALESCE(SUM(f.amount) FILTER (WHERE f.status = 'success'), 0)     AS gtv,
    COALESCE(SUM(r.refunded), 0)                                AS refund_amount,
    COUNT(r.transaction_id)                                     AS refunded_transactions,
    COALESCE(SUM(f.fee_amount) FILTER (WHERE f.status = 'success'), 0) AS fee_revenue,
    COALESCE(SUM(ROUND(f.fee_amount * r.refunded / f.amount, 2)), 0)   AS fee_reversed
FROM f LEFT JOIN r ON r.transaction_id = f.transaction_id
"""


def get_kpis(start, end, merchant=None, method=None) -> dict:
    k = fetch_one(KPI_SQL, _params(start, end, merchant, method))
    k["net_transaction_value"] = k["gtv"] - k["refund_amount"]
    k["net_revenue"] = k["fee_revenue"] - k["fee_reversed"]
    k["refund_rate"] = _div(k["refund_amount"], k["gtv"])
    k["refund_count_rate"] = _div(k["refunded_transactions"], k["successful"])
    k["aov"] = _div(k["gtv"], k["successful"])
    k["success_rate"] = _div(k["successful"], k["successful"] + k["failed"])
    return k


TREND_SQL = f"""
WITH buckets AS (   -- every period in the range, so days with no sales show as 0
    SELECT generate_series(date_trunc(%(granularity)s, %(start)s::date::timestamp),
                           date_trunc(%(granularity)s, %(end)s::date::timestamp),
                           ('1 ' || %(granularity)s)::interval)::date AS bucket
),
f AS (
    SELECT t.*, date_trunc(%(granularity)s, t.created_at AT TIME ZONE %(tz)s)::date AS bucket
    FROM transactions t WHERE {FILTER}
),
r AS (
    SELECT rf.transaction_id, SUM(rf.amount) AS refunded
    FROM refunds rf JOIN f ON f.transaction_id = rf.transaction_id AND f.status = 'success'
    GROUP BY rf.transaction_id
),
agg AS (
    SELECT f.bucket,
           COUNT(*)                                                AS transactions,
           COUNT(*) FILTER (WHERE f.status = 'success')            AS successful,
           COALESCE(SUM(f.amount) FILTER (WHERE f.status = 'success'), 0)     AS gtv,
           COALESCE(SUM(r.refunded), 0)                            AS refund_amount,
           COALESCE(SUM(f.fee_amount) FILTER (WHERE f.status = 'success'), 0)
             - COALESCE(SUM(ROUND(f.fee_amount * r.refunded / f.amount, 2)), 0) AS net_revenue
    FROM f LEFT JOIN r ON r.transaction_id = f.transaction_id
    GROUP BY f.bucket
)
SELECT b.bucket,
       COALESCE(a.transactions, 0)  AS transactions,
       COALESCE(a.successful, 0)    AS successful,
       COALESCE(a.gtv, 0)           AS gtv,
       COALESCE(a.refund_amount, 0) AS refund_amount,
       COALESCE(a.gtv, 0) - COALESCE(a.refund_amount, 0) AS net_transaction_value,
       COALESCE(a.net_revenue, 0)   AS net_revenue
FROM buckets b LEFT JOIN agg a ON a.bucket = b.bucket
ORDER BY b.bucket
"""


def _period_end(bucket: date, granularity: str) -> date:
    if granularity == "day":
        return bucket
    if granularity == "week":
        return bucket + timedelta(days=6)
    first_of_next = date(bucket.year + bucket.month // 12, bucket.month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def get_trends(start, end, granularity, merchant=None, method=None) -> list[dict]:
    rows = fetch_all(TREND_SQL, _params(start, end, merchant, method, granularity=granularity))
    for r in rows:  # a period cut off by the date filter would otherwise look like a drop
        r["partial"] = r["bucket"] < start or _period_end(r["bucket"], granularity) > end
    return rows


def get_breakdowns(start, end, merchant=None, method=None) -> dict:
    p = _params(start, end, merchant, method)
    by_status = fetch_all(f"""
        SELECT t.status, COUNT(*) AS transactions, SUM(t.amount) AS value
        FROM transactions t WHERE {FILTER} GROUP BY t.status ORDER BY transactions DESC""", p)
    by_method = fetch_all(f"""
        SELECT t.payment_method, COUNT(*) AS successful, SUM(t.amount) AS gtv
        FROM transactions t WHERE {FILTER} AND t.status = 'success'
        GROUP BY t.payment_method ORDER BY gtv DESC""", p)
    by_merchant = fetch_all(f"""
        SELECT t.merchant_id, COUNT(*) AS successful, SUM(t.amount) AS gtv,
               COALESCE(SUM(rf.refunded), 0) AS refund_amount
        FROM transactions t
        LEFT JOIN (SELECT transaction_id, SUM(amount) AS refunded FROM refunds GROUP BY transaction_id) rf
               ON rf.transaction_id = t.transaction_id
        WHERE {FILTER} AND t.status = 'success'
        GROUP BY t.merchant_id ORDER BY gtv DESC""", p)
    return {"by_status": by_status, "by_payment_method": by_method, "by_merchant": by_merchant}


SORTS = {"created_at": "t.created_at", "amount": "t.amount"}


def list_transactions(start, end, merchant=None, method=None, status=None, search=None,
                      flagged_only=False, sort="created_at", order="desc", page=1, page_size=25) -> dict:
    # Sort column and direction come from a fixed whitelist, never from raw input.
    order_by = f"{SORTS[sort]} {'ASC' if order == 'asc' else 'DESC'}, t.transaction_id"
    p = _params(start, end, merchant, method, status=status,
                search=f"%{search}%" if search else None, flagged=flagged_only,
                limit=page_size, offset=(page - 1) * page_size)
    rows = fetch_all(f"""
        SELECT t.transaction_id, t.customer_id, t.merchant_id, t.amount, t.fee_amount, t.status,
               t.payment_method, t.created_at,
               COALESCE(rf.refunded, 0) AS refunded_amount,
               COALESCE(fl.flags, ARRAY[]::text[]) AS flags,
               COUNT(*) OVER () AS total_count
        FROM transactions t
        LEFT JOIN (SELECT transaction_id, SUM(amount) AS refunded FROM refunds GROUP BY transaction_id) rf
               ON rf.transaction_id = t.transaction_id
        LEFT JOIN (SELECT transaction_id, array_agg(rule_code ORDER BY rule_code) AS flags
                   FROM anomaly_flags GROUP BY transaction_id) fl
               ON fl.transaction_id = t.transaction_id
        WHERE {FILTER}
          AND (%(status)s::text IS NULL OR t.status = %(status)s)
          AND (%(search)s::text IS NULL OR t.transaction_id ILIKE %(search)s OR t.customer_id ILIKE %(search)s)
          AND (NOT %(flagged)s OR fl.flags IS NOT NULL)
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s""", p)
    total = rows[0]["total_count"] if rows else 0
    for r in rows:
        del r["total_count"]
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


def get_transaction(transaction_id: str) -> dict | None:
    txn = fetch_one("SELECT * FROM transactions WHERE transaction_id = %(id)s", {"id": transaction_id})
    if txn is None:
        return None
    txn["refunds"] = fetch_all("""
        SELECT refund_id, amount, refunded_at, reason FROM refunds
        WHERE transaction_id = %(id)s ORDER BY refunded_at""", {"id": transaction_id})
    txn["flags"] = fetch_all("""
        SELECT rule_code, severity, explanation FROM anomaly_flags
        WHERE transaction_id = %(id)s ORDER BY rule_code""", {"id": transaction_id})
    txn["refunded_amount"] = sum((r["amount"] for r in txn["refunds"]), Decimal("0"))
    return txn


def get_anomalies(start, end, merchant=None, method=None) -> dict:
    p = _params(start, end, merchant, method)
    summary = fetch_all(f"""
        SELECT a.rule_code, a.severity, COUNT(*) AS flagged, SUM(t.amount) AS amount_at_risk
        FROM anomaly_flags a JOIN transactions t ON t.transaction_id = a.transaction_id
        WHERE {FILTER} GROUP BY a.rule_code, a.severity ORDER BY flagged DESC""", p)
    items = fetch_all(f"""
        SELECT a.transaction_id, a.rule_code, a.severity, a.explanation,
               t.customer_id, t.merchant_id, t.amount, t.status, t.created_at
        FROM anomaly_flags a JOIN transactions t ON t.transaction_id = a.transaction_id
        WHERE {FILTER}
        ORDER BY CASE a.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.created_at DESC
        LIMIT 200""", p)
    return {"summary": summary, "items": items}


def get_data_quality() -> dict:
    summary = fetch_all("""
        SELECT source_file, issue_code, COUNT(*) AS issues
        FROM data_quality_issues GROUP BY source_file, issue_code ORDER BY issues DESC""")
    items = fetch_all("""
        SELECT source_file, row_number, record_id, issue_code, message
        FROM data_quality_issues ORDER BY source_file DESC, row_number LIMIT 200""")
    runs = fetch_all("""
        SELECT dataset_label, is_synthetic, rows_received, rows_loaded, rows_rejected, loaded_at
        FROM ingestion_runs ORDER BY id DESC LIMIT 5""")
    return {"summary": summary, "items": items, "runs": runs}
