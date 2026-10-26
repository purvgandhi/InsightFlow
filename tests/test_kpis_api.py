"""KPI correctness against a tiny hand-calculated dataset, through the real API + SQL.

Reporting timezone is Asia/Kolkata (UTC+5:30). Range under test: 2026-06-01 .. 2026-06-30.

  id   status     amount   fee    date (IST)         refunds
  T1   success    1000.00  20.00  Jun 01 10:00       400.00 (partial)
  T2   success     500.00   0.00  Jun 01 11:00       500.00 (full)
  T3   success     250.00   5.00  Jun 01 12:00
  T4   failed      300.00   0.00  Jun 01 13:00
  T5   pending     200.00   0.00  Jun 02 09:00
  T6   cancelled   100.00   0.00  Jun 02 09:30
  T7   success     750.00  15.00  Jun 02 10:00
  T9   success     100.00   0.00  Jun 30 23:30  <- 18:00 UTC, still June 30 in IST
  T8   success     999.00  10.00  Jul 15 10:00       999.00  <- outside range
  T10  success      50.00   0.00  May 31 23:59  <- 18:29 UTC May 31, outside range

  total_transactions = 8 (T1-T7, T9)      successful = 5 (T1 T2 T3 T7 T9)
  gtv          = 1000 + 500 + 250 + 750 + 100 = 2600.00
  refund       = 400 + 500 = 900.00  (T8's refund is outside the range)
  net value    = 2600 - 900 = 1700.00
  fee revenue  = 20 + 0 + 5 + 15 + 0 = 40.00
  fee reversed = 20 * 400/1000 + 0 = 8.00  ->  net revenue = 32.00
  refund rate  = 900 / 2600 = 0.346153...
  aov          = 2600 / 5 = 520.00
  success rate = 5 / (5 + 1 failed) = 0.8333...
"""

import pytest
from fastapi.testclient import TestClient

ROWS = [
    ("T1", "success", 1000.00, 20.00, "2026-06-01 10:00+05:30"),
    ("T2", "success", 500.00, 0.00, "2026-06-01 11:00+05:30"),
    ("T3", "success", 250.00, 5.00, "2026-06-01 12:00+05:30"),
    ("T4", "failed", 300.00, 0.00, "2026-06-01 13:00+05:30"),
    ("T5", "pending", 200.00, 0.00, "2026-06-02 09:00+05:30"),
    ("T6", "cancelled", 100.00, 0.00, "2026-06-02 09:30+05:30"),
    ("T7", "success", 750.00, 15.00, "2026-06-02 10:00+05:30"),
    ("T8", "success", 999.00, 10.00, "2026-07-15 10:00+05:30"),
    ("T9", "success", 100.00, 0.00, "2026-06-30 23:30+05:30"),
    ("T10", "success", 50.00, 0.00, "2026-05-31 23:59+05:30"),
]
REFUNDS = [("R1", "T1", 400.00, "2026-06-03 10:00+05:30"),
           ("R2", "T2", 500.00, "2026-06-01 12:00+05:30"),
           ("R3", "T8", 999.00, "2026-07-16 10:00+05:30")]
JUNE = {"start": "2026-06-01", "end": "2026-06-30"}


@pytest.fixture(scope="module")
def client(db_conn):
    db_conn.execute("TRUNCATE anomaly_flags, refunds, transactions, data_quality_issues, ingestion_runs")
    with db_conn.cursor() as cur:
        cur.executemany("""INSERT INTO transactions (transaction_id, customer_id, merchant_id, amount,
                           fee_amount, status, payment_method, created_at)
                           VALUES (%s, 'C1', 'M1', %s, %s, %s, 'card', %s)""",
                        [(t, a, f, s, ts) for t, s, a, f, ts in ROWS])
        cur.executemany("INSERT INTO refunds (refund_id, transaction_id, amount, refunded_at) VALUES (%s, %s, %s, %s)",
                        REFUNDS)
        cur.execute("""INSERT INTO anomaly_flags (transaction_id, rule_code, severity, explanation)
                       VALUES ('T2', 'QUICK_FULL_REFUND', 'low', 'test flag')""")
    import app
    with TestClient(app.app) as c:
        yield c


def test_kpis_match_hand_calculation(client):
    k = client.get("/api/kpis", params=JUNE).json()["kpis"]
    assert k["total_transactions"] == 8
    assert (k["successful"], k["failed"], k["pending"], k["cancelled"]) == (5, 1, 1, 1)
    assert k["gtv"] == 2600.00
    assert k["refund_amount"] == 900.00
    assert k["refunded_transactions"] == 2
    assert k["net_transaction_value"] == 1700.00
    assert k["fee_revenue"] == 40.00
    assert k["net_revenue"] == 32.00
    assert k["refund_rate"] == pytest.approx(900 / 2600)
    assert k["aov"] == pytest.approx(520.00)
    assert k["success_rate"] == pytest.approx(5 / 6)


def test_daily_trend_buckets_in_reporting_timezone(client):
    series = client.get("/api/trends", params={**JUNE, "granularity": "day"}).json()["series"]
    assert len(series) == 30                                   # zero-filled, one per day
    by_day = {s["bucket"]: s for s in series}
    assert by_day["2026-06-01"]["gtv"] == 1750.00              # T1 + T2 + T3
    assert by_day["2026-06-01"]["refund_amount"] == 900.00     # refunds follow the original payment date
    assert by_day["2026-06-02"]["gtv"] == 750.00
    assert by_day["2026-06-30"]["gtv"] == 100.00               # T9, late evening IST
    assert by_day["2026-06-15"]["gtv"] == 0
    assert sum(s["gtv"] for s in series) == 2600.00            # matches the KPI card


def test_monthly_trend_and_filters(client):
    series = client.get("/api/trends", params={"start": "2026-05-01", "end": "2026-07-31",
                                               "granularity": "month"}).json()["series"]
    assert [s["gtv"] for s in series] == [50.00, 2600.00, 999.00]
    k = client.get("/api/kpis", params={**JUNE, "merchant": "OTHER"}).json()["kpis"]
    assert k["total_transactions"] == 0 and k["aov"] is None and k["refund_rate"] is None


def test_transactions_table_and_drilldown(client):
    page = client.get("/api/transactions", params={**JUNE, "status": "success", "sort": "amount",
                                                   "order": "desc", "page_size": 2}).json()
    assert page["total"] == 5
    assert [t["transaction_id"] for t in page["items"]] == ["T1", "T7"]
    assert page["items"][0]["refunded_amount"] == 400.00
    flagged = client.get("/api/transactions", params={**JUNE, "flagged_only": True}).json()
    assert [t["transaction_id"] for t in flagged["items"]] == ["T2"]
    assert client.get("/api/transactions", params={**JUNE, "q": "t9"}).json()["total"] == 1
    detail = client.get("/api/transactions/T1").json()
    assert detail["refunded_amount"] == 400.00 and detail["refunds"][0]["refund_id"] == "R1"
    assert client.get("/api/transactions/NOPE").status_code == 404


def test_invalid_requests_are_rejected(client):
    assert client.get("/api/kpis", params={"start": "2026-07-01", "end": "2026-06-01"}).status_code == 422
    assert client.get("/api/kpis", params={"start": "not-a-date", "end": "2026-06-01"}).status_code == 422
    assert client.get("/api/trends", params={**JUNE, "granularity": "year"}).status_code == 422
    assert client.get("/api/transactions", params={**JUNE, "page_size": 1000}).status_code == 422
    assert client.get("/api/transactions", params={**JUNE, "sort": "amount; DROP TABLE x"}).status_code == 422


def test_database_outage_returns_503(monkeypatch, client):
    from insightflow import db
    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:wrong@127.0.0.1:1/none")
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["error"] == "database_unavailable"
    assert "wrong" not in r.text  # credentials never leak into responses
    db.close_pool()
