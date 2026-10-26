"""InsightFlow API (FastAPI). Vercel detects this file and runs `app` as a Python Function.

Run locally:  uvicorn app:app --reload   then open http://127.0.0.1:8000
"""

import io
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Literal

import pandas as pd
import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from insightflow import queries
from insightflow.db import DatabaseNotConfigured, close_pool, fetch_one, get_pool
from insightflow.pipeline import run_pipeline
from insightflow.validation import TXN_REQUIRED, validate_transactions

try:  # local development convenience; on Vercel env vars come from Project Settings
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("insightflow")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    close_pool()


app = FastAPI(title="InsightFlow API", version="1.0.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)

MAX_RANGE_DAYS = 3 * 366
Status = Literal["success", "failed", "pending", "cancelled"]


# ---- Error handling: never leak connection strings or stack traces to the browser ----

@app.exception_handler(DatabaseNotConfigured)
async def _not_configured(_: Request, exc: Exception):
    return JSONResponse(status_code=503, content={"error": "database_not_configured",
                                                  "message": "DATABASE_URL is not set on the server."})


@app.exception_handler(psycopg.errors.QueryCanceled)
async def _timeout(_: Request, exc: Exception):
    return JSONResponse(status_code=504, content={"error": "query_timeout",
                                                  "message": "The query took too long. Try a shorter date range."})


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(PoolTimeout)
async def _db_down(_: Request, exc: Exception):
    log.error("Database unavailable: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"error": "database_unavailable",
                                                  "message": "Cannot reach the database right now. Please retry shortly."})


@app.exception_handler(psycopg.Error)
async def _db_error(_: Request, exc: Exception):
    log.exception("Database error")
    return JSONResponse(status_code=500, content={"error": "database_error", "message": "Unexpected database error."})


# ---- Shared query filters ----

class Filters:
    def __init__(
        self,
        start: date | None = Query(None, description="Inclusive start date (YYYY-MM-DD)"),
        end: date | None = Query(None, description="Inclusive end date (YYYY-MM-DD)"),
        merchant: str | None = Query(None, max_length=64),
        method: Literal["upi", "card", "netbanking", "wallet"] | None = Query(None),
    ):
        if start is None or end is None:
            bounds = fetch_one(f"""SELECT (MIN(created_at) AT TIME ZONE %(tz)s)::date AS lo,
                                          (MAX(created_at) AT TIME ZONE %(tz)s)::date AS hi
                                   FROM transactions""", {"tz": queries.TZ})
            start = start or bounds["lo"] or date.today()
            end = end or bounds["hi"] or date.today()
        if start > end:
            raise HTTPException(422, "start must be on or before end")
        if (end - start).days > MAX_RANGE_DAYS:
            raise HTTPException(422, f"Date range cannot exceed {MAX_RANGE_DAYS} days")
        self.start, self.end, self.merchant, self.method = start, end, merchant, method

    def kwargs(self) -> dict:
        return {"start": self.start, "end": self.end, "merchant": self.merchant, "method": self.method}


def _with_range(f: Filters, payload: dict) -> dict:
    return {"filters": f.kwargs(), **payload}


# ---- Routes ----

@app.get("/api/health")
def health():
    fetch_one("SELECT 1 AS ok")
    return {"status": "ok", "database": "connected"}


@app.get("/api/meta")
def meta():
    return queries.get_meta()


@app.get("/api/kpis")
def kpis(f: Filters = Depends()):
    return _with_range(f, {"kpis": queries.get_kpis(**f.kwargs())})


@app.get("/api/trends")
def trends(f: Filters = Depends(), granularity: Literal["day", "week", "month"] = "day"):
    return _with_range(f, {"granularity": granularity,
                           "series": queries.get_trends(granularity=granularity, **f.kwargs())})


@app.get("/api/breakdowns")
def breakdowns(f: Filters = Depends()):
    return _with_range(f, queries.get_breakdowns(**f.kwargs()))


@app.get("/api/transactions")
def transactions(
    f: Filters = Depends(),
    status: Status | None = None,
    q: str | None = Query(None, max_length=64, description="Search transaction or customer ID"),
    flagged_only: bool = False,
    sort: Literal["created_at", "amount"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(25, ge=1, le=100),
):
    return _with_range(f, queries.list_transactions(
        status=status, search=q.strip() if q and q.strip() else None, flagged_only=flagged_only,
        sort=sort, order=order, page=page, page_size=page_size, **f.kwargs()))


@app.get("/api/transactions/{transaction_id}")
def transaction_detail(transaction_id: str):
    if len(transaction_id) > 64:
        raise HTTPException(422, "transaction_id is too long")
    txn = queries.get_transaction(transaction_id)
    if txn is None:
        raise HTTPException(404, f"Transaction {transaction_id} not found")
    return txn


@app.get("/api/anomalies")
def anomalies(f: Filters = Depends()):
    return _with_range(f, queries.get_anomalies(**f.kwargs()))


@app.get("/api/data-quality")
def data_quality():
    return queries.get_data_quality()


MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB limit


@app.post("/api/import/preview")
async def preview_csv(file: UploadFile = File(...)):
    """Inspect and validate an uploaded CSV without saving to the database."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Uploaded file exceeds the maximum 5MB size limit.")
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(422, "The uploaded CSV file is empty.")

    try:
        raw_df = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False, na_values=[""])
    except Exception as e:
        raise HTTPException(422, f"Failed to parse CSV: {str(e)}")

    if raw_df.empty:
        raise HTTPException(422, "The uploaded CSV contains no data rows.")

    found_cols = [c.strip() for c in raw_df.columns]
    raw_df.columns = found_cols
    missing_cols = [c for c in TXN_REQUIRED if c not in found_cols]

    clean_df, issues_df = validate_transactions(raw_df)

    issues_by_code = issues_df["issue_code"].value_counts().to_dict() if not issues_df.empty else {}
    issues_sample = []
    if not issues_df.empty:
        for r in issues_df.head(20).itertuples():
            issues_sample.append({
                "row_number": int(r.row_number),
                "record_id": str(r.record_id or ""),
                "issue_code": str(r.issue_code),
                "message": str(r.message),
            })

    # Sample rows for table preview
    sample_rows = (clean_df if not clean_df.empty else raw_df).head(8)
    preview_rows = sample_rows.to_dict("records")

    return {
        "filename": file.filename,
        "total_rows": len(raw_df),
        "valid_rows": len(clean_df),
        "rejected_rows": int(issues_df["row_number"].nunique()) if not issues_df.empty else 0,
        "columns": found_cols,
        "missing_columns": missing_cols,
        "issues_by_code": issues_by_code,
        "issues_sample": issues_sample,
        "preview_rows": preview_rows,
        "can_import": len(clean_df) > 0 and len(missing_cols) == 0,
    }


@app.post("/api/import")
async def import_csv(
    file: UploadFile = File(...),
    mode: Literal["replace", "append"] = Form("replace"),
    dataset_label: str = Form(None)
):
    """Validate and import transactions into PostgreSQL, recalculating anomalies and metrics."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Uploaded file exceeds the maximum 5MB size limit.")
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise HTTPException(422, "The uploaded CSV file is empty.")

    try:
        raw_df = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False, na_values=[""])
    except Exception as e:
        raise HTTPException(422, f"Failed to parse CSV: {str(e)}")

    found_cols = [c.strip() for c in raw_df.columns]
    raw_df.columns = found_cols
    missing_cols = [c for c in TXN_REQUIRED if c not in found_cols]
    if missing_cols:
        raise HTTPException(422, f"Missing required columns: {', '.join(missing_cols)}")

    clean_df, _ = validate_transactions(raw_df)
    if clean_df.empty:
        raise HTTPException(422, "All rows in the CSV failed validation. Check the preview for details.")

    label = (dataset_label or f"Import: {file.filename}").strip()

    with get_pool().connection() as conn:
        summary = run_pipeline(
            conn=conn,
            transactions_csv=raw_df,
            refunds_csv=None,
            dataset_label=label,
            is_synthetic=False,
            reset=(mode == "replace")
        )

    return {
        "status": "success",
        "dataset_label": label,
        "mode": mode,
        "filename": file.filename,
        "summary": summary,
    }


@app.post("/api/reset-demo")
def reset_demo():
    """Restore the database to the 6-month synthetic demo dataset."""
    demo_txns = Path(__file__).parent / "data" / "sample_transactions.csv"
    demo_refunds = Path(__file__).parent / "data" / "sample_refunds.csv"
    if not demo_txns.exists() or not demo_refunds.exists():
        raise HTTPException(500, "Demo dataset files not found on server.")

    with get_pool().connection() as conn:
        summary = run_pipeline(
            conn=conn,
            transactions_csv=demo_txns,
            refunds_csv=demo_refunds,
            dataset_label="Synthetic demo dataset (Apr to Sep 2026)",
            is_synthetic=True,
            reset=True
        )

    return {
        "status": "success",
        "message": "Dashboard restored to synthetic demo dataset (4,633 transactions).",
        "summary": summary,
    }



# Locally, serve the dashboard from ./public. On Vercel, public/ is served by the CDN.
if not os.environ.get("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "public", html=True), name="public")
