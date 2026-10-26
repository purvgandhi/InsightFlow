"""Generate the SYNTHETIC demo dataset in data/.

Everything here is fake. The script is deterministic (fixed seed) so the
dashboard numbers are reproducible, and it deliberately plants data-quality
defects and suspicious patterns so the validation and anomaly rules have
something to find.

    python scripts/generate_sample_data.py
"""

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 42
IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 4, 1, tzinfo=IST)
DAYS = 183  # Apr 1 – Sep 30, 2026
OUT = Path(__file__).resolve().parent.parent / "data"

MERCHANTS = {  # merchant_id: (typical ticket size in INR, daily volume weight)
    "M001-GROCERMART": (650, 5),
    "M002-QUICKBITES": (420, 6),
    "M003-ELECTROHUB": (9500, 1),
    "M004-STYLESTREET": (2200, 2),
    "M005-TRAVELNEST": (7800, 1),
    "M006-PHARMAPLUS": (900, 3),
    "M007-EDULEARN": (3500, 1),
    "M008-FUELGO": (1500, 3),
}
# payment_method: (share of attempts, platform fee rate). UPI is zero-MDR in India.
METHODS = {"upi": (0.55, 0.0), "card": (0.28, 0.02), "netbanking": (0.10, 0.015), "wallet": (0.07, 0.018)}
STATUSES = (["success"] * 86) + (["failed"] * 8) + (["pending"] * 4) + (["cancelled"] * 2)
REFUND_REASONS = ["customer_request", "item_not_delivered", "duplicate_payment", "damaged_item"]

TXN_FIELDS = ["transaction_id", "customer_id", "merchant_id", "amount", "fee_amount",
              "currency", "status", "payment_method", "created_at"]
REFUND_FIELDS = ["refund_id", "transaction_id", "amount", "refunded_at", "reason"]


def iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def make_txn(rng, n, customer, merchant, amount, method, status, ts):
    fee = round(amount * METHODS[method][1], 2) if status == "success" else 0.0
    return {
        "transaction_id": f"TXN{n:06d}", "customer_id": customer, "merchant_id": merchant,
        "amount": f"{amount:.2f}", "fee_amount": f"{fee:.2f}", "currency": "INR",
        "status": status, "payment_method": method, "created_at": iso(ts),
    }


def main() -> None:
    rng = random.Random(SEED)
    customers = [f"C{i:04d}" for i in range(1, 901)]
    merchant_ids = list(MERCHANTS)
    weights = [w for _, w in MERCHANTS.values()]
    methods = list(METHODS)
    method_weights = [s for s, _ in METHODS.values()]

    txns, refunds = [], []
    n = 0

    def next_id():
        nonlocal n
        n += 1
        return n

    # 1. Normal traffic, gently growing over time with weekend bumps.
    for day in range(DAYS):
        date = START + timedelta(days=day)
        volume = int(18 + day * 0.06 + (6 if date.weekday() >= 5 else 0) + rng.randint(-4, 4))
        for _ in range(volume):
            merchant = rng.choices(merchant_ids, weights)[0]
            base = MERCHANTS[merchant][0]
            amount = round(max(20.0, rng.lognormvariate(0, 0.45) * base), 2)
            ts = date + timedelta(seconds=rng.randint(7 * 3600, 23 * 3600))
            method = rng.choices(methods, method_weights)[0]
            txns.append(make_txn(rng, next_id(), rng.choice(customers), merchant,
                                 amount, method, rng.choice(STATUSES), ts))

    # 2. Refunds on ~6% of successful payments (mix of full and partial).
    r = 0
    for t in txns:
        if t["status"] == "success" and rng.random() < 0.06:
            r += 1
            amount = float(t["amount"])
            refund = amount if rng.random() < 0.6 else round(amount * rng.uniform(0.2, 0.8), 2)
            ts = datetime.fromisoformat(t["created_at"]) + timedelta(hours=rng.randint(6, 240))
            refunds.append({"refund_id": f"RF{r:05d}", "transaction_id": t["transaction_id"],
                            "amount": f"{refund:.2f}", "refunded_at": iso(ts),
                            "reason": rng.choice(REFUND_REASONS)})

    # 3. Planted suspicious patterns (valid records that anomaly rules should flag).
    for i in range(5):  # possible double charge: same customer/merchant/amount 40s apart
        ts = START + timedelta(days=20 + i * 30, hours=13)
        cust, merch = customers[100 + i], "M002-QUICKBITES"
        for k in range(2):
            txns.append(make_txn(rng, next_id(), cust, merch, 349.0, "card", "success",
                                 ts + timedelta(seconds=40 * k)))
    for i in range(3):  # card testing: burst of failed attempts in a few minutes
        ts = START + timedelta(days=45 + i * 40, hours=2)
        for k in range(5):
            txns.append(make_txn(rng, next_id(), f"C9{i:03d}", "M003-ELECTROHUB",
                                 round(rng.uniform(1, 15), 2) + 20, "card", "failed",
                                 ts + timedelta(seconds=50 * k)))
    for i in range(6):  # unusually large ticket for the merchant
        merch = ["M001-GROCERMART", "M002-QUICKBITES", "M006-PHARMAPLUS"][i % 3]
        txns.append(make_txn(rng, next_id(), rng.choice(customers), merch,
                             MERCHANTS[merch][0] * rng.uniform(12, 20), "card", "success",
                             START + timedelta(days=15 + i * 27, hours=22)))
    for i in range(4):  # full refund within minutes of payment
        t = make_txn(rng, next_id(), rng.choice(customers), "M004-STYLESTREET", 5400.0,
                     "card", "success", START + timedelta(days=30 + i * 35, hours=18))
        txns.append(t)
        r += 1
        refunds.append({"refund_id": f"RF{r:05d}", "transaction_id": t["transaction_id"],
                        "amount": t["amount"],
                        "refunded_at": iso(datetime.fromisoformat(t["created_at"]) + timedelta(minutes=12)),
                        "reason": "customer_request"})

    # 4. Planted data-quality defects (should be rejected by validation).
    defects = []
    sample = rng.sample([t for t in txns if t["status"] == "success"], 40)
    for t in sample[:4]:
        defects.append({**t, "amount": ""})                       # missing amount
    for t in sample[4:7]:
        defects.append({**t, "customer_id": ""})                  # missing customer
    for t in sample[7:11]:
        defects.append({**t, "amount": "-" + t["amount"]})        # negative amount
    for t in sample[11:13]:
        defects.append({**t, "amount": "12O0.00"})                # non-numeric amount
    for t in sample[13:16]:
        defects.append({**t, "created_at": "2026-13-45T10:00:00+05:30"})  # invalid timestamp
    for t in sample[16:18]:
        defects.append({**t, "created_at": "2027-03-01T10:00:00+05:30"})  # future timestamp
    for t in sample[18:22]:
        defects.append({**t, "status": "completed"})              # unknown status
    for i, t in enumerate(defects):                               # give each defect its own id
        t["transaction_id"] = f"TXNBAD{i + 1:03d}"
    for t in sample[22:32]:
        defects.append(dict(t))                                   # exact duplicate row
    for t in sample[32:35]:
        defects.append({**t, "amount": f"{float(t['amount']) + 100:.2f}"})  # same id, different amount
    # A few valid rows with messy formatting the cleaner should normalise.
    for t in sample[35:40]:
        t["status"] = "  SUCCESS "
        t["payment_method"] = t["payment_method"].upper()
    txns.extend(defects)

    successful = [t for t in txns if t["status"] == "success" and not t["transaction_id"].startswith("TXNBAD")]
    failed = [t for t in txns if t["status"] == "failed"]
    bad_refunds = [
        {"refund_id": "RFBAD01", "transaction_id": "TXN999999", "amount": "500.00",
         "refunded_at": "2026-06-01T12:00:00+05:30", "reason": "customer_request"},  # unknown transaction
        {"refund_id": "RFBAD02", "transaction_id": failed[10]["transaction_id"], "amount": "100.00",
         "refunded_at": "2026-06-02T12:00:00+05:30", "reason": "customer_request"},  # refund of a failed payment
        {"refund_id": "RFBAD03", "transaction_id": successful[200]["transaction_id"],
         "amount": f"{float(successful[200]['amount']) * 2:.2f}",
         "refunded_at": iso(datetime.fromisoformat(successful[200]["created_at"]) + timedelta(days=1)),
         "reason": "customer_request"},                                              # exceeds payment
        {"refund_id": "RFBAD04", "transaction_id": successful[300]["transaction_id"], "amount": "10.00",
         "refunded_at": iso(datetime.fromisoformat(successful[300]["created_at"]) - timedelta(days=2)),
         "reason": "customer_request"},                                              # before payment
        {"refund_id": "RFBAD05", "transaction_id": successful[400]["transaction_id"], "amount": "",
         "refunded_at": iso(datetime.fromisoformat(successful[400]["created_at"]) + timedelta(days=1)),
         "reason": "customer_request"},                                              # missing amount
    ]
    refunds.extend(bad_refunds)
    refunds.append(dict(refunds[0]))  # duplicate refund row

    rng.shuffle(txns)
    OUT.mkdir(exist_ok=True)
    with open(OUT / "sample_transactions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, TXN_FIELDS)
        w.writeheader()
        w.writerows(txns)
    with open(OUT / "sample_refunds.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, REFUND_FIELDS)
        w.writeheader()
        w.writerows(refunds)
    print(f"Wrote {len(txns)} transaction rows and {len(refunds)} refund rows to {OUT}")


if __name__ == "__main__":
    main()
