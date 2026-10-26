import io
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_preview_empty_file_returns_422():
    resp = client.post("/api/import/preview", files={"file": ("empty.csv", b"", "text/csv")})
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower()


def test_preview_missing_required_columns():
    csv_missing = b"transaction_id,amount\nTXN1,100\n"
    resp = client.post("/api/import/preview", files={"file": ("missing.csv", csv_missing, "text/csv")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_import"] is False
    assert "customer_id" in data["missing_columns"]
    assert "merchant_id" in data["missing_columns"]


def test_preview_invalid_amounts_and_dates():
    csv_invalid = b"""transaction_id,customer_id,merchant_id,amount,fee_amount,currency,status,payment_method,created_at
TXN1,C1,M1,-50,0,INR,success,card,2026-10-15T10:00:00Z
TXN2,C2,M1,abc,0,INR,success,card,2026-10-15T10:00:00Z
TXN3,C3,M1,100,0,INR,unknown_status,card,2026-10-15T10:00:00Z
TXN4,C4,M1,200,0,INR,success,card,invalid_date
TXN5,C5,M1,500,10,INR,success,card,2026-10-15T10:00:00Z
"""
    resp = client.post("/api/import/preview", files={"file": ("invalid.csv", csv_invalid, "text/csv")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid_rows"] == 1
    assert data["rejected_rows"] == 4
    assert "INVALID_AMOUNT" in data["issues_by_code"]
    assert "INVALID_STATUS" in data["issues_by_code"]
    assert "INVALID_TIMESTAMP" in data["issues_by_code"]


def test_preview_duplicate_transaction_ids():
    csv_dup = b"""transaction_id,customer_id,merchant_id,amount,fee_amount,currency,status,payment_method,created_at
TXN1,C1,M1,100,0,INR,success,card,2026-10-15T10:00:00Z
TXN1,C1,M1,200,0,INR,success,card,2026-10-15T10:00:00Z
TXN2,C2,M1,300,0,INR,success,card,2026-10-15T10:00:00Z
"""
    resp = client.post("/api/import/preview", files={"file": ("dup.csv", csv_dup, "text/csv")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid_rows"] == 1
    assert data["rejected_rows"] == 2
    assert "DUPLICATE_ID_CONFLICT" in data["issues_by_code"]


def test_preview_sample_transactions_csv():
    with open("public/sample_transactions.csv", "rb") as f:
        sample_bytes = f.read()
    resp = client.post("/api/import/preview", files={"file": ("sample_transactions.csv", sample_bytes, "text/csv")})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_rows"] == 25
    assert data["valid_rows"] == 25
    assert data["rejected_rows"] == 0
    assert data["can_import"] is True
    assert len(data["missing_columns"]) == 0
