import urllib.request
import json
import sys

BASE = "https://fintech-three-sand.vercel.app"

def post_multipart(url, filename, content, data=None):
    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    body = bytearray()
    if data:
        for k, v in data.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode())
    body.extend(content)
    body.extend(f'\r\n--{boundary}--\r\n'.encode())
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "Live-Verification/1.0"
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

print("--- Live Verification on https://fintech-three-sand.vercel.app ---")

# 1. Download sample_transactions.csv
req = urllib.request.Request(f"{BASE}/sample_transactions.csv")
with urllib.request.urlopen(req) as resp:
    sample_csv = resp.read()
    print("1. Sample CSV Download: HTTP", resp.status, f"({len(sample_csv)} bytes, {len(sample_csv.decode().splitlines())} lines)")
    assert resp.status == 200
    assert len(sample_csv.decode().splitlines()) == 26

# 2. Preview Empty CSV
status, data = post_multipart(f"{BASE}/api/import/preview", "empty.csv", b"")
print("2. Preview Empty CSV: HTTP", status, data.get("detail"))
assert status == 422

# 3. Preview Missing Columns
status, data = post_multipart(f"{BASE}/api/import/preview", "missing.csv", b"transaction_id,amount\nTXN1,100\n")
print("3. Preview Missing Cols: HTTP", status, "Missing:", data.get("missing_columns"), "Can import:", data.get("can_import"))
assert status == 200
assert data.get("can_import") is False

# 4. Preview Invalid Amounts/Dates
invalid_csv = b"""transaction_id,customer_id,merchant_id,amount,fee_amount,currency,status,payment_method,created_at
TXN1,C1,M1,-50,0,INR,success,card,2026-10-15T10:00:00Z
TXN2,C2,M1,abc,0,INR,success,card,2026-10-15T10:00:00Z
TXN3,C3,M1,100,0,INR,unknown_status,card,2026-10-15T10:00:00Z
TXN4,C4,M1,200,0,INR,success,card,invalid_date
TXN5,C5,M1,500,10,INR,success,card,2026-10-15T10:00:00Z
"""
status, data = post_multipart(f"{BASE}/api/import/preview", "invalid.csv", invalid_csv)
print("4. Preview Invalid: HTTP", status, "Valid:", data.get("valid_rows"), "Rejected:", data.get("rejected_rows"), "Issues:", data.get("issues_by_code"))
assert status == 200
assert data.get("valid_rows") == 1
assert data.get("rejected_rows") == 4

# 5. Preview Sample Transactions CSV
status, data = post_multipart(f"{BASE}/api/import/preview", "sample_transactions.csv", sample_csv)
print("5. Preview Sample CSV: HTTP", status, "Total:", data.get("total_rows"), "Valid:", data.get("valid_rows"), "Can import:", data.get("can_import"))
assert status == 200
assert data.get("total_rows") == 25
assert data.get("valid_rows") == 25
assert data.get("can_import") is True

# 6. Execute Import
status, data = post_multipart(f"{BASE}/api/import", "sample_transactions.csv", sample_csv, {"mode": "replace"})
print("6. Execute Import: HTTP", status, data.get("status"), "Dataset:", data.get("dataset_label"))
assert status == 200

# 7. Verify Dashboard KPIs after import
req = urllib.request.Request(f"{BASE}/api/kpis")
with urllib.request.urlopen(req) as resp:
    kpis = json.loads(resp.read().decode())["kpis"]
    print("7. Recalculated KPIs: Total txns =", kpis["total_transactions"], "| GTV =", kpis["gtv"], "| Success rate =", round(kpis["success_rate"]*100, 2), "%")
    assert kpis["total_transactions"] == 25

# 8. Reset to Demo Data
req = urllib.request.Request(f"{BASE}/api/reset-demo", data=b"", method="POST")
with urllib.request.urlopen(req) as resp:
    reset_data = json.loads(resp.read().decode())
    print("8. Reset to Demo Data: HTTP", resp.status, reset_data.get("message"))
    assert resp.status == 200

# 9. Verify 4,633 Demo transactions restored
req = urllib.request.Request(f"{BASE}/api/kpis")
with urllib.request.urlopen(req) as resp:
    kpis = json.loads(resp.read().decode())["kpis"]
    print("9. Restored Demo KPIs: Total txns =", kpis["total_transactions"], "| GTV =", kpis["gtv"])
    assert kpis["total_transactions"] >= 4600

print("\n>>> ALL 9 LIVE PRODUCTION VERIFICATION TESTS PASSED SUCCESSFULLY! <<<")
