"""
tools.py — All tool / function implementations for the Travel Reimbursement Agent.

Each tool is a plain Python function with:
  - A clear docstring describing what it does
  - Typed arguments
  - A dict return value with structured results

Tools perform DETERMINISTIC operations — no LLM calls happen here.
The LLM decides which tools to call; these functions do the actual work.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
POLICY_DIR = BASE_DIR / "data" / "policy"
UPLOADS_DIR = BASE_DIR / "data" / "uploads"

# ── Load policy data once ────────────────────────────────────────────────────
_limits_cache = None


def _load_limits() -> dict:
    """Load and cache limits.json."""
    global _limits_cache
    if _limits_cache is None:
        limits_path = POLICY_DIR / "limits.json"
        with open(limits_path, "r") as f:
            _limits_cache = json.load(f)
    return _limits_cache


def _load_policy_text() -> str:
    """Load the full travel_policy.md text."""
    policy_path = POLICY_DIR / "travel_policy.md"
    with open(policy_path, "r") as f:
        return f.read()


def _normalize_category(category: str) -> str:
    """Normalize a category name using aliases from limits.json."""
    limits = _load_limits()
    cat = category.lower().strip().replace(" ", "_")

    # Direct match
    if cat in limits["categories"]:
        return cat

    # Check aliases
    aliases = limits.get("category_aliases", {})
    if cat in aliases:
        return aliases[cat]

    # Fuzzy: check if category contains a known key
    for key in limits["categories"]:
        if key in cat or cat in key:
            return key

    return cat  # Return as-is if no match


def _determine_travel_type(city: str, currency: str) -> str:
    """Determine if travel is domestic or international based on city/currency."""
    domestic_indicators = ["INR", "₹"]
    if currency.upper() in domestic_indicators:
        return "domestic"

    international_indicators = ["USD", "$", "EUR", "GBP"]
    if currency.upper() in international_indicators:
        return "international"

    # Heuristic: Indian city names
    indian_cities = [
        "mumbai", "delhi", "bangalore", "bengaluru", "chennai", "hyderabad",
        "pune", "kolkata", "ahmedabad", "jaipur", "lucknow", "goa",
        "chandigarh", "kochi", "indore", "nagpur", "surat", "noida",
        "gurgaon", "gurugram",
    ]
    if city.lower().strip() in indian_cities:
        return "domestic"

    return "international"


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 1: policy_lookup
# ═══════════════════════════════════════════════════════════════════════════════

def policy_lookup(category: str, currency: str = "INR", city: str = "") -> dict:
    """
    Look up the relevant policy rules and limits for a given expense category.

    Args:
        category: Expense category (e.g., "airfare", "hotel", "meals").
        currency: Currency code to determine domestic vs international.
        city: City name to help determine travel type.

    Returns:
        dict with keys:
            - category: normalized category name
            - travel_type: "domestic" or "international"
            - limits: daily_limit, trip_limit, currency, requires_receipt_above, notes
            - policy_sections: list of relevant policy section summaries
            - found: bool indicating if the category was found
    """
    logger.info("TOOL: policy_lookup(category=%s, currency=%s, city=%s)", category, currency, city)

    limits = _load_limits()
    norm_cat = _normalize_category(category)
    travel_type = _determine_travel_type(city, currency)

    result = {
        "tool": "policy_lookup",
        "category": norm_cat,
        "original_category": category,
        "travel_type": travel_type,
        "limits": None,
        "policy_sections": [],
        "found": False,
    }

    if norm_cat in limits["categories"]:
        cat_limits = limits["categories"][norm_cat].get(travel_type)
        if cat_limits:
            result["limits"] = cat_limits
            result["found"] = True
            result["policy_sections"] = [
                f"Section 3: Per-Category Limits ({travel_type})",
                "Section 2: Receipt Requirements",
                "Section 5: Pre-Approval Requirements",
            ]
    else:
        result["policy_sections"] = [
            f"Category '{category}' not found in policy. "
            "Eligible categories: airfare, hotel, meals, local_transport, per_diem, miscellaneous."
        ]

    # Add general policy info
    result["receipt_threshold"] = limits["receipt_required_above"].get(
        "INR" if travel_type == "domestic" else "USD", 500
    )
    result["pre_approval_threshold"] = limits["pre_approval_required_above"].get(
        "INR" if travel_type == "domestic" else "USD", 20000
    )
    result["submission_deadline_days"] = limits["submission_deadline_days"]
    result["late_submission_max_days"] = limits["late_submission_max_days"]

    logger.info("TOOL: policy_lookup result — found=%s, limits=%s", result["found"], result["limits"])
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 2: check_receipt_completeness
# ═══════════════════════════════════════════════════════════════════════════════

def check_receipt_completeness(
    claim_id: str,
    claimed_amount: float,
    currency: str,
    upload_dir: str | None = None,
) -> dict:
    """
    Check whether a PDF receipt was uploaded for the claim, extract key fields
    from the PDF, and flag missing/illegible/mismatched receipts.

    Args:
        claim_id: The claim identifier to match receipts against.
        claimed_amount: The amount claimed (for comparison with receipt).
        currency: Currency of the claim.
        upload_dir: Override directory to look for receipts.

    Returns:
        dict with:
            - has_receipt: bool
            - receipt_files: list of matched filenames
            - extracted_data: {vendor, amount, date} from each receipt
            - issues: list of issue descriptions
            - receipt_required: bool (based on amount vs threshold)
    """
    logger.info("TOOL: check_receipt_completeness(claim_id=%s, amount=%s %s)", claim_id, claimed_amount, currency)

    search_dir = Path(upload_dir) if upload_dir else UPLOADS_DIR
    limits = _load_limits()

    # Determine receipt threshold
    threshold = limits["receipt_required_above"].get(currency.upper(), 500)
    receipt_required = claimed_amount > threshold

    result = {
        "tool": "check_receipt_completeness",
        "claim_id": claim_id,
        "has_receipt": False,
        "receipt_files": [],
        "extracted_data": [],
        "issues": [],
        "receipt_required": receipt_required,
        "receipt_threshold": threshold,
        "currency": currency,
        "is_receipt_missing_error": False,
    }

    if not search_dir.exists():
        if receipt_required:
            result["is_receipt_missing_error"] = True
            result["issues"].append(
                f"No upload directory found. Receipt is REQUIRED for amounts above {threshold} {currency}."
            )
        logger.info("TOOL: check_receipt_completeness — upload dir not found: %s", search_dir)
        return result

    # Find matching receipt files (match by claim_id in filename)
    matched_files = []
    for pdf in search_dir.glob("*.pdf"):
        # Match exactly 'claimid.pdf' or 'claimid_something.pdf' to avoid CLM003 matching CLM003B
        pdf_name = pdf.name.lower()
        target = str(claim_id).lower()
        if pdf_name == f"{target}.pdf" or pdf_name.startswith(f"{target}_"):
            matched_files.append(pdf)

    if not matched_files:
        result["has_receipt"] = False
        if receipt_required:
            result["is_receipt_missing_error"] = True
            result["issues"].append(
                f"MISSING RECEIPT: No PDF receipt found for claim {claim_id}. "
                f"Receipt is mandatory for amounts above {threshold} {currency}."
            )
        else:
            result["issues"].append(
                f"No receipt found, but amount ({claimed_amount} {currency}) is below "
                f"the receipt threshold ({threshold} {currency}). Written description is sufficient."
            )
        logger.info("TOOL: check_receipt_completeness — no receipts found for %s", claim_id)
        return result

    result["has_receipt"] = True
    result["receipt_files"] = [f.name for f in matched_files]

    # Extract data from each receipt PDF
    for pdf_path in matched_files:
        extracted = _extract_receipt_data(pdf_path)
        result["extracted_data"].append(extracted)

        # Check for mismatches
        if extracted.get("amount") is not None:
            receipt_amount = extracted["amount"]
            if abs(receipt_amount - claimed_amount) > 0.01:
                if receipt_amount < claimed_amount:
                    result["issues"].append(
                        f"AMOUNT MISMATCH: Receipt shows {receipt_amount} {currency} "
                        f"but claim is for {claimed_amount} {currency}. "
                        f"Receipt amount is LESS than claimed amount."
                    )
                else:
                    result["issues"].append(
                        f"AMOUNT MISMATCH: Receipt shows {receipt_amount} {currency} "
                        f"but claim is for {claimed_amount} {currency}."
                    )

        if extracted.get("date") and extracted.get("claim_date_mismatch"):
            result["issues"].append(
                f"DATE MISMATCH: Receipt date ({extracted['date']}) does not match claim date."
            )

        if not extracted.get("vendor"):
            result["issues"].append("Receipt vendor name could not be extracted (possibly illegible).")

    logger.info(
        "TOOL: check_receipt_completeness — found %d receipts, %d issues",
        len(matched_files), len(result["issues"]),
    )
    return result


def _extract_receipt_data(pdf_path: Path) -> dict:
    """
    Extract vendor, amount, and date from a PDF receipt.
    Uses pdfplumber if available, falls back to pypdf.
    """
    text = ""
    try:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
        except ImportError:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        logger.warning("Failed to extract text from %s: %s", pdf_path, e)
        return {"vendor": None, "amount": None, "date": None, "raw_text": "", "extraction_error": str(e)}

    # Parse extracted text for key fields
    extracted = {
        "vendor": _extract_vendor(text),
        "amount": _extract_amount(text),
        "date": _extract_date(text),
        "raw_text": text[:500],  # Truncate for logging
        "claim_date_mismatch": False,
    }

    return extracted


def _extract_vendor(text: str) -> str | None:
    """Try to extract vendor/company name from receipt text."""
    if not text.strip():
        return None

    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if lines:
        # Usually the vendor/company name is on the first non-empty line
        vendor = lines[0]
        # Clean up: remove common prefixes
        vendor = re.sub(r"^(Receipt|Invoice|Bill|Tax Invoice)[:\s]*", "", vendor, flags=re.IGNORECASE).strip()
        if vendor and len(vendor) > 1:
            return vendor

    return None


def _extract_amount(text: str) -> float | None:
    """Try to extract the total/final amount from receipt text."""
    if not text.strip():
        return None

    # Look for "Total", "Grand Total", "Amount", "Net Amount" patterns
    patterns = [
        r"(?:grand\s+total|total\s+amount|net\s+amount|total|amount\s+due|amount\s+payable)[:\s]*[₹$]?\s*([\d,]+\.?\d*)",
        r"(?:total|amount)[:\s]*(?:INR|USD|Rs\.?|₹|\$)\s*([\d,]+\.?\d*)",
        r"[₹$]\s*([\d,]+\.?\d*)",
        r"(?:INR|USD|Rs\.?)\s*([\d,]+\.?\d*)",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            try:
                # Take the last match (usually the total at the bottom)
                amount_str = matches[-1].replace(",", "")
                return float(amount_str)
            except ValueError:
                continue

    return None


def _extract_date(text: str) -> str | None:
    """Try to extract a date from receipt text."""
    if not text.strip():
        return None

    # Common date patterns
    patterns = [
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
        r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
        r"(?:Date|Dated?)[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 3: check_limit
# ═══════════════════════════════════════════════════════════════════════════════

def check_limit(
    category: str,
    amount: float,
    currency: str = "INR",
    city: str = "",
) -> dict:
    """
    Compare a claimed amount against the policy limit for that category.

    Args:
        category: Expense category.
        amount: Claimed amount.
        currency: Currency code.
        city: City name (for domestic vs international).

    Returns:
        dict with:
            - within_limit: bool
            - claimed_amount: float
            - daily_limit: float or None
            - trip_limit: float or None
            - excess_amount: float (0 if within limit)
            - approved_amount: float (capped at limit if exceeded)
            - percent_of_limit: float
            - triggers_manual_review: bool (if > 150% of limit)
    """
    logger.info("TOOL: check_limit(category=%s, amount=%s %s)", category, amount, currency)

    limits = _load_limits()
    norm_cat = _normalize_category(category)
    travel_type = _determine_travel_type(city, currency)
    manual_review_pct = limits.get("manual_review_excess_percent", 150)

    result = {
        "tool": "check_limit",
        "category": norm_cat,
        "travel_type": travel_type,
        "claimed_amount": amount,
        "currency": currency,
        "daily_limit": None,
        "trip_limit": None,
        "applicable_limit": None,
        "within_limit": True,
        "excess_amount": 0.0,
        "approved_amount": amount,
        "percent_of_limit": 0.0,
        "triggers_manual_review": False,
        "category_found": False,
    }

    if norm_cat not in limits["categories"]:
        result["within_limit"] = False
        result["approved_amount"] = 0
        result["excess_amount"] = amount
        logger.info("TOOL: check_limit — category '%s' not found in policy", norm_cat)
        return result

    cat_limits = limits["categories"][norm_cat].get(travel_type)
    if not cat_limits:
        logger.info("TOOL: check_limit — no %s limits for category '%s'", travel_type, norm_cat)
        return result

    result["category_found"] = True
    result["daily_limit"] = cat_limits.get("daily_limit")
    result["trip_limit"] = cat_limits.get("trip_limit")

    # Use daily limit for comparison (more granular); fall back to trip limit
    applicable_limit = cat_limits.get("daily_limit") or cat_limits.get("trip_limit")
    result["applicable_limit"] = applicable_limit

    if applicable_limit is not None and applicable_limit > 0:
        result["percent_of_limit"] = round((amount / applicable_limit) * 100, 1)

        if amount > applicable_limit:
            result["within_limit"] = False
            result["excess_amount"] = round(amount - applicable_limit, 2)
            result["approved_amount"] = applicable_limit

            if result["percent_of_limit"] > manual_review_pct:
                result["triggers_manual_review"] = True
        else:
            result["within_limit"] = True
            result["excess_amount"] = 0.0
            result["approved_amount"] = amount

    logger.info(
        "TOOL: check_limit result — within_limit=%s, excess=%s, pct=%s%%",
        result["within_limit"], result["excess_amount"], result["percent_of_limit"],
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 4: detect_duplicate
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_text(value):
    """Normalize text for comparison."""
    if value is None:
        return ""
    return str(value).strip().lower()


def _normalize_date(value):
    """Normalize dates to YYYY-MM-DD."""
    if value is None:
        return ""

    if isinstance(value, datetime):
        return value.date().isoformat()

    value = str(value).strip()

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except Exception:
            pass

    return value


def detect_duplicate(claim_id: str, all_claims: list[dict]) -> dict:
    """
    Detect duplicate claims.

    Duplicate Criteria:
    - employee_id
    - category
    - amount
    - date
    - city
    - description
    """

    logger.info(
        "TOOL: detect_duplicate(claim_id=%s, total_claims=%d)",
        claim_id,
        len(all_claims),
    )

    result = {
        "tool": "detect_duplicate",
        "claim_id": claim_id,
        "is_duplicate": False,
        "duplicate_of": [],
        "match_fields": [],
    }

    # Find current claim
    current = None
    current_index = -1

    for idx, claim in enumerate(all_claims):
        if str(claim.get("claim_id")).strip() == str(claim_id).strip():
            current = claim
            current_index = idx
            break

    if current is None:
        logger.warning("Claim %s not found", claim_id)
        return result

    current_employee = _normalize_text(current.get("employee_id"))
    current_category = _normalize_text(current.get("category"))
    current_city = _normalize_text(current.get("city"))
    current_description = _normalize_text(current.get("description"))
    current_date = _normalize_date(current.get("date"))

    try:
        current_amount = float(current.get("amount", 0))
    except Exception:
        current_amount = 0.0

    # Compare only with earlier claims
    for idx, other in enumerate(all_claims):

        if idx >= current_index:
            continue

        matches = []

        if current_employee == _normalize_text(other.get("employee_id")):
            matches.append("employee_id")

        if current_category == _normalize_text(other.get("category")):
            matches.append("category")

        if current_city == _normalize_text(other.get("city")):
            matches.append("city")

        if current_description == _normalize_text(other.get("description")):
            matches.append("description")

        if current_date == _normalize_date(other.get("date")):
            matches.append("date")

        try:
            other_amount = float(other.get("amount", 0))
        except Exception:
            other_amount = 0.0

        if abs(current_amount - other_amount) < 0.01:
            matches.append("amount")

        logger.info(
            "Comparing %s with %s -> %s",
            claim_id,
            other.get("claim_id"),
            matches,
        )

        if len(matches) == 6:
            result["is_duplicate"] = True
            result["duplicate_of"].append(str(other.get("claim_id")))
            result["match_fields"] = matches

    logger.info("Duplicate Detection Result: %s", result)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 5: validate_output
# ═══════════════════════════════════════════════════════════════════════════════

VALID_DECISIONS = {"Approve", "Partially Approve", "Reject", "Manual Review"}
REQUIRED_FIELDS = [
    "claim_id", "decision", "claimed_amount", "approved_amount",
    "deducted_amount", "missing_documents", "policy_reference",
    "confidence", "explanation",
]


def validate_output(decision_json: dict) -> dict:
    """
    Schema and sanity validator for the agent's structured output.

    Checks:
      - All required fields are present
      - Amounts are non-negative
      - approved_amount <= claimed_amount
      - decision is one of the 4 allowed values
      - confidence is between 0 and 1
      - missing_documents and policy_reference are lists

    Args:
        decision_json: The structured decision dict to validate.

    Returns:
        dict with:
            - valid: bool
            - errors: list of error strings
            - corrected: dict with auto-corrected values (if possible)
    """
    logger.info("TOOL: validate_output(claim_id=%s)", decision_json.get("claim_id", "unknown"))

    errors = []
    corrected = dict(decision_json)

    # Check required fields
    for field in REQUIRED_FIELDS:
        if field not in decision_json:
            errors.append(f"Missing required field: '{field}'")

    # Validate decision value
    decision = decision_json.get("decision", "")
    if decision not in VALID_DECISIONS:
        errors.append(f"Invalid decision '{decision}'. Must be one of: {VALID_DECISIONS}")
        corrected["decision"] = "Manual Review"

    # Validate amounts
    claimed = decision_json.get("claimed_amount", 0)
    approved = decision_json.get("approved_amount", 0)
    deducted = decision_json.get("deducted_amount", 0)

    try:
        claimed = float(claimed)
        corrected["claimed_amount"] = claimed
    except (TypeError, ValueError):
        errors.append("claimed_amount must be numeric")
        claimed = 0
        corrected["claimed_amount"] = 0

    try:
        if approved is None:
            approved = 0
        approved = float(approved)
        corrected["approved_amount"] = approved
    except (TypeError, ValueError):
        errors.append("approved_amount must be numeric")
        approved = 0
        corrected["approved_amount"] = 0

    try:
        if deducted is None:
            deducted = 0
        deducted = float(deducted)
        corrected["deducted_amount"] = deducted
    except (TypeError, ValueError):
        errors.append("deducted_amount must be numeric")
        deducted = 0
        corrected["deducted_amount"] = 0

    if claimed < 0:
        errors.append(f"claimed_amount ({claimed}) must be non-negative")
    if approved < 0:
        errors.append(f"approved_amount ({approved}) must be non-negative")
        corrected["approved_amount"] = 0
    if deducted < 0:
        errors.append(f"deducted_amount ({deducted}) must be non-negative")
        corrected["deducted_amount"] = 0

    if approved > claimed and claimed > 0:
        errors.append(f"approved_amount ({approved}) exceeds claimed_amount ({claimed})")
        corrected["approved_amount"] = claimed
        corrected["deducted_amount"] = 0

    # Auto-correct deducted_amount
    final_claimed = corrected.get("claimed_amount", 0)
    final_approved = corrected.get("approved_amount", 0)
    final_deducted = corrected.get("deducted_amount", 0)
    if abs((final_claimed - final_approved) - final_deducted) > 0.01:
        corrected["deducted_amount"] = round(final_claimed - final_approved, 2)

    # Validate confidence
    confidence = decision_json.get("confidence", 0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0
        errors.append("confidence must be a number")

    if not (0 <= confidence <= 1):
        errors.append(f"confidence ({confidence}) must be between 0 and 1")
        corrected["confidence"] = max(0, min(1, confidence))

    # Validate list fields
    for list_field in ["missing_documents", "policy_reference"]:
        val = decision_json.get(list_field)
        if val is not None and not isinstance(val, list):
            errors.append(f"'{list_field}' must be a list")
            corrected[list_field] = [str(val)] if val else []

    # Ensure missing_documents and policy_reference exist
    if "missing_documents" not in corrected:
        corrected["missing_documents"] = []
    if "policy_reference" not in corrected:
        corrected["policy_reference"] = []

    result = {
        "tool": "validate_output",
        "valid": len(errors) == 0,
        "errors": errors,
        "corrected": corrected,
    }

    logger.info("TOOL: validate_output — valid=%s, errors=%d", result["valid"], len(errors))
    return result
