"""Create the schema and import transaction/refund CSVs into PostgreSQL.

    python scripts/load_data.py                       # load the synthetic demo dataset
    python scripts/load_data.py --reset               # wipe tables first
    python scripts/load_data.py --transactions my_txns.csv --refunds my_refunds.csv --label "June export" --real

Uses DATABASE_URL_UNPOOLED if set (recommended for bulk loads on Neon), else DATABASE_URL.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from insightflow.pipeline import run_pipeline  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transactions", default=ROOT / "data" / "sample_transactions.csv")
    ap.add_argument("--refunds", default=ROOT / "data" / "sample_refunds.csv")
    ap.add_argument("--label", default="Synthetic demo dataset (Apr to Sep 2026)")
    ap.add_argument("--real", action="store_true", help="mark the data as real (not synthetic)")
    ap.add_argument("--reset", action="store_true", help="truncate all tables before loading")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL (or DATABASE_URL_UNPOOLED) in your environment or .env file.")

    with psycopg.connect(url, prepare_threshold=None) as conn:
        summary = run_pipeline(conn, args.transactions, args.refunds, args.label,
                               is_synthetic=not args.real, reset=args.reset)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
