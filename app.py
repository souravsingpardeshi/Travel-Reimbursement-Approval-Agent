"""
app.py — Flask application for the Travel Reimbursement Approval Agent.

Routes:
  /          — Upload form (Excel claims + PDF receipts)
  /run       — POST: triggers the agent loop, redirects to results
  /results   — Results table with decision badges and expandable audit trail
  /status    — JSON endpoint for Ollama health check
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import openpyxl
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

from agent import orchestrator, llm_client

# ── Configuration ────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
OUTPUTS_DIR = BASE_DIR / "outputs"
ALLOWED_EXTENSIONS_EXCEL = {".xlsx", ".xls"}
ALLOWED_EXTENSIONS_PDF = {".pdf"}

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-25s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "travel-reimburse-demo-key-2025")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload

# Store the latest run results in memory (no database)
_latest_results = []
_latest_run_meta = {}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_excel(filepath: str) -> list[dict]:
    """
    Parse an Excel file into a list of claim dicts.
    Expected columns: claim_id, employee_id, category, amount, currency, date, city, description
    """
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # First row is header
    headers = [str(h).strip().lower().replace(" ", "_") if h else f"col_{i}"
               for i, h in enumerate(rows[0])]

    claims = []
    for row in rows[1:]:
        if not any(row):  # Skip fully empty rows
            continue
        claim = {}
        for i, val in enumerate(row):
            if i < len(headers):
                key = headers[i]
                # Convert datetime objects to strings
                if hasattr(val, "strftime"):
                    val = val.strftime("%Y-%m-%d")
                claim[key] = val
        # Ensure required fields
        if claim.get("claim_id") is not None:
            claim["claim_id"] = str(claim["claim_id"]).strip()
            claim["amount"] = float(claim.get("amount", 0) or 0)
            claim["currency"] = str(claim.get("currency", "INR")).strip()
            claims.append(claim)

    wb.close()
    return claims


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Upload form page."""
    # Check Ollama status for UI indicator
    ollama_status = llm_client.check_ollama_status()
    return render_template("index.html", ollama_status=ollama_status)


@app.route("/run", methods=["POST"])
def run_agent():
    """Handle file uploads and trigger the agent pipeline."""
    global _latest_results, _latest_run_meta

    # ── Validate Excel upload ────────────────────────────────────────────
    excel_file = request.files.get("claims_file")
    if not excel_file or not excel_file.filename:
        flash("Please upload an Excel file with claims.", "error")
        return redirect(url_for("index"))

    ext = Path(excel_file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS_EXCEL:
        flash(f"Invalid file type '{ext}'. Please upload .xlsx or .xls.", "error")
        return redirect(url_for("index"))

    # ── Save uploads ─────────────────────────────────────────────────────
    # Clean previous uploads
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Save Excel
    excel_path = UPLOAD_DIR / secure_filename(excel_file.filename)
    excel_file.save(str(excel_path))
    logger.info("Saved claims file: %s", excel_path)

    # Save receipt PDFs
    receipt_files = request.files.getlist("receipt_files")
    receipt_count = 0
    for rf in receipt_files:
        if rf and rf.filename:
            rf_ext = Path(rf.filename).suffix.lower()
            if rf_ext in ALLOWED_EXTENSIONS_PDF:
                rf_path = UPLOAD_DIR / secure_filename(rf.filename)
                rf.save(str(rf_path))
                receipt_count += 1
                logger.info("Saved receipt: %s", rf_path)

    # ── Parse claims ─────────────────────────────────────────────────────
    try:
        claims = _parse_excel(str(excel_path))
    except Exception as e:
        logger.exception("Failed to parse Excel file")
        flash(f"Failed to parse Excel file: {e}", "error")
        return redirect(url_for("index"))

    if not claims:
        flash("No valid claims found in the Excel file.", "error")
        return redirect(url_for("index"))

    logger.info("Parsed %d claims from Excel", len(claims))

    # ── Run the agent ────────────────────────────────────────────────────
    start_time = datetime.now()
    try:
        results = orchestrator.process_claims(claims, upload_dir=str(UPLOAD_DIR))
    except Exception as e:
        logger.exception("Agent pipeline failed")
        flash(f"Agent pipeline error: {e}", "error")
        return redirect(url_for("index"))

    elapsed = (datetime.now() - start_time).total_seconds()

    # ── Store results ────────────────────────────────────────────────────
    _latest_results = results
    _latest_run_meta = {
        "timestamp": start_time.isoformat(),
        "claims_file": excel_file.filename,
        "total_claims": len(claims),
        "receipts_uploaded": receipt_count,
        "processing_time_seconds": round(elapsed, 1),
    }

    # Also save to outputs directory
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    run_output = {
        "meta": _latest_run_meta,
        "results": results,
    }
    output_path = OUTPUTS_DIR / f"run_{start_time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_path, "w") as f:
        json.dump(run_output, f, indent=2, default=str)

    flash(
        f"Processed {len(results)} claims in {elapsed:.1f}s. "
        f"{receipt_count} receipt(s) analyzed.",
        "success",
    )
    return redirect(url_for("results"))


@app.route("/results")
def results():
    """Display the results table."""
    # Compute summary stats
    summary = {"Approve": 0, "Partially Approve": 0, "Reject": 0, "Manual Review": 0}
    total_claimed = 0.0
    total_approved = 0.0

    for r in _latest_results:
        decision = r.get("decision", "Manual Review")
        summary[decision] = summary.get(decision, 0) + 1
        
        # Safe float conversion to handle None or invalid types
        try:
            claimed = r.get("claimed_amount")
            total_claimed += float(claimed) if claimed is not None else 0.0
        except (ValueError, TypeError):
            pass
            
        try:
            approved = r.get("approved_amount")
            total_approved += float(approved) if approved is not None else 0.0
        except (ValueError, TypeError):
            pass

    return render_template(
        "results.html",
        results=_latest_results,
        meta=_latest_run_meta,
        summary=summary,
        total_claimed=total_claimed,
        total_approved=total_approved,
    )


@app.route("/status")
def status():
    """JSON health check endpoint."""
    ollama = llm_client.check_ollama_status()
    return jsonify({
        "app": "Travel Reimbursement Agent",
        "ollama": ollama,
        "latest_run": _latest_run_meta or None,
    })


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    logger.info("Starting Travel Reimbursement Agent on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)
