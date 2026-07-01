"""
orchestrator.py — Core agentic loop for the Travel Reimbursement Agent.

This module is the "brain" of the agent. For each claim it:
  1. Loads claim data + matched receipt PDFs
  2. Runs deterministic tool calls (receipt check, limit check, duplicate, policy lookup)
  3. Passes claim + tool results + policy context into the LLM
  4. Validates the LLM's JSON response
  5. Saves the full audit trail to outputs/<claim_id>.json

The workflow is explicit and readable — no black-box chains.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from agent import llm_client, tools, prompts

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = BASE_DIR / "outputs"
POLICY_DIR = BASE_DIR / "data" / "policy"


def process_claims(claims: list[dict], upload_dir: str | None = None) -> list[dict]:
    """
    Process a batch of travel reimbursement claims through the agent pipeline.

    Args:
        claims: List of claim dicts (from Excel parsing).
        upload_dir: Directory containing uploaded receipt PDFs.

    Returns:
        List of structured decision dicts, one per claim.
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    total = len(claims)

    for idx, claim in enumerate(claims, 1):
        claim_id = str(claim.get("claim_id", f"unknown_{idx}"))
        logger.info("=" * 70)
        logger.info("Processing claim %d/%d: %s", idx, total, claim_id)
        logger.info("=" * 70)

        try:
            decision = _process_single_claim(claim, claims, upload_dir)
        except Exception as e:
            logger.exception("Unexpected error processing claim %s", claim_id)
            decision = _make_manual_review_fallback(
                claim, f"Unexpected processing error: {e}"
            )

        results.append(decision)

        # Save audit trail
        _save_audit_trail(claim_id, decision)

    return results


def _process_single_claim(
    claim: dict, all_claims: list[dict], upload_dir: str | None
) -> dict:
    """
    Process a single claim through the full agent pipeline.

    Steps:
      1. Run deterministic tools
      2. Build LLM prompt with tool results
      3. Call LLM for decision
      4. Validate output
      5. Return structured decision with audit trail
    """
    claim_id = str(claim.get("claim_id", "unknown"))
    category = str(claim.get("category", "")).strip()
    amount = _safe_float(claim.get("amount", 0))
    currency = str(claim.get("currency", "INR")).strip()
    city = str(claim.get("city", "")).strip()

    audit_trail = {
        "claim_input": claim,
        "tool_calls": [],
        "llm_call": None,
        "validation": None,
        "timestamp": datetime.now().isoformat(),
    }

    tools_used = []

    # ── Step 1: Run deterministic tools ──────────────────────────────────

    # Tool 1: Policy Lookup
    logger.info("[%s] Running policy_lookup...", claim_id)
    policy_result = tools.policy_lookup(category, currency, city)
    audit_trail["tool_calls"].append({
        "tool": "policy_lookup",
        "input": {"category": category, "currency": currency, "city": city},
        "output": policy_result,
    })
    tools_used.append("policy_lookup")

    # Tool 2: Receipt Completeness Check
    logger.info("[%s] Running check_receipt_completeness...", claim_id)
    receipt_result = tools.check_receipt_completeness(
        claim_id, amount, currency, upload_dir
    )
    audit_trail["tool_calls"].append({
        "tool": "check_receipt_completeness",
        "input": {"claim_id": claim_id, "amount": amount, "currency": currency},
        "output": receipt_result,
    })
    tools_used.append("check_receipt_completeness")

    # Tool 3: Limit Check
    logger.info("[%s] Running check_limit...", claim_id)
    limit_result = tools.check_limit(category, amount, currency, city)
    audit_trail["tool_calls"].append({
        "tool": "check_limit",
        "input": {"category": category, "amount": amount, "currency": currency},
        "output": limit_result,
    })
    tools_used.append("check_limit")

    # Tool 4: Duplicate Detection
    logger.info("[%s] Running detect_duplicate...", claim_id)
    duplicate_result = tools.detect_duplicate(claim_id, all_claims)
    audit_trail["tool_calls"].append({
        "tool": "detect_duplicate",
        "input": {"claim_id": claim_id},
        "output": duplicate_result,
    })
    tools_used.append("detect_duplicate")

    # ── Step 2: Load policy text for context ─────────────────────────────

    policy_text = _get_relevant_policy_sections(category)

    # ── Step 3: Build prompt and call LLM ────────────────────────────────

    user_prompt = prompts.DECISION_PROMPT_TEMPLATE.format(
        claim_id=claim_id,
        employee_id=claim.get("employee_id", "N/A"),
        category=category,
        amount=amount,
        currency=currency,
        date=claim.get("date", "N/A"),
        city=city,
        description=claim.get("description", "N/A"),
        policy_lookup_result=json.dumps(policy_result, indent=2, default=str),
        receipt_check_result=json.dumps(receipt_result, indent=2, default=str),
        limit_check_result=json.dumps(limit_result, indent=2, default=str),
        duplicate_check_result=json.dumps(duplicate_result, indent=2, default=str),
        policy_text=policy_text,
    )

    logger.info("[%s] Calling LLM for decision...", claim_id)
    llm_response = llm_client.chat(
        system_prompt=prompts.SYSTEM_PROMPT,
        user_prompt=user_prompt,
        json_mode=True,
    )

    audit_trail["llm_call"] = {
        "model": llm_response.get("model"),
        "duration_ms": llm_response.get("duration_ms"),
        "raw_content": llm_response.get("content"),
        "error": llm_response.get("error"),
    }

    # ── Step 4: Handle LLM response ─────────────────────────────────────

    if llm_response.get("error") or llm_response.get("parsed_json") is None:
        logger.warning(
            "[%s] LLM call failed or returned invalid JSON: %s",
            claim_id, llm_response.get("error"),
        )
        decision = _make_manual_review_fallback(
            claim,
            f"LLM unavailable or returned invalid response, routed to manual review. "
            f"Error: {llm_response.get('error', 'Invalid JSON response')}",
        )
        decision["tools_used"] = tools_used + ["validate_output"]
        audit_trail["validation"] = {"valid": False, "errors": ["LLM failure fallback"]}
        audit_trail["final_decision"] = decision
        return decision

    llm_decision = llm_response["parsed_json"]

    # Ensure claim_id is correct (don't trust LLM)
    llm_decision["claim_id"] = claim_id
    llm_decision["claimed_amount"] = amount  # Use actual amount, not LLM's

    # ── Step 5: Validate output ──────────────────────────────────────────

    logger.info("[%s] Running validate_output...", claim_id)
    validation = tools.validate_output(llm_decision)
    audit_trail["validation"] = validation
    tools_used.append("validate_output")

    if validation["valid"]:
        decision = llm_decision
        logger.info("[%s] Validation passed. Decision: %s", claim_id, decision.get("decision"))
    else:
        logger.warning(
            "[%s] Validation failed with %d errors: %s",
            claim_id, len(validation["errors"]), validation["errors"],
        )
        # Use corrected version if available, otherwise fallback
        if validation.get("corrected"):
            decision = validation["corrected"]
            # If errors are severe, force Manual Review
            severe_errors = [e for e in validation["errors"] if "Invalid decision" in e or "Missing required" in e]
            if severe_errors:
                decision["decision"] = "Manual Review"
                decision["explanation"] = (
                    f"Original LLM decision failed validation ({', '.join(validation['errors'])}). "
                    "Routed to manual review for safety."
                )
        else:
            decision = _make_manual_review_fallback(
                claim,
                f"LLM output failed validation: {', '.join(validation['errors'])}",
            )

    decision["tools_used"] = tools_used
    decision["receipt_files"] = receipt_result.get("receipt_files", [])

    # Safety override for LLM hallucinations
    if duplicate_result.get("is_duplicate"):
        logger.warning(
            "[%s] Duplicate detected. Overriding LLM decision.",
            claim_id,
        )

        decision["decision"] = "Reject"
        decision["approved_amount"] = 0.0
        decision["deducted_amount"] = decision["claimed_amount"]
        decision["confidence"] = 1.0
        decision["explanation"] = (
            f"Duplicate claim detected. Duplicate of claim(s): "
            f"{', '.join(duplicate_result['duplicate_of'])}."
        )

    if receipt_result.get("is_receipt_missing_error") and decision["decision"] in ["Approve", "Partially Approve"]:
        logger.warning("[%s] SAFETY OVERRIDE: LLM approved claim with missing receipt. Forcing Manual Review.", claim_id)
        decision["decision"] = "Manual Review"
        decision["approved_amount"] = 0.0
        decision["deducted_amount"] = decision["claimed_amount"]
        decision["explanation"] = "Safety Override: Missing required receipt."

    audit_trail["final_decision"] = decision

    return decision


def _make_manual_review_fallback(claim: dict, reason: str) -> dict:
    """Create a Manual Review decision as a safe fallback."""
    amount = _safe_float(claim.get("amount", 0))
    return {
        "claim_id": str(claim.get("claim_id", "unknown")),
        "decision": "Manual Review",
        "claimed_amount": amount,
        "approved_amount": 0.0,
        "deducted_amount": amount,
        "missing_documents": [],
        "policy_reference": [],
        "confidence": 0.0,
        "explanation": reason,
        "tools_used": [],
    }


def _get_relevant_policy_sections(category: str) -> str:
    """Load and return the relevant sections of the travel policy."""
    try:
        policy_path = POLICY_DIR / "travel_policy.md"
        with open(policy_path, "r") as f:
            full_text = f.read()
        # Return a trimmed version to fit context window
        # For a small model, we limit the policy text
        if len(full_text) > 4000:
            return full_text[:4000] + "\n... (policy text truncated)"
        return full_text
    except Exception as e:
        logger.warning("Failed to load policy text: %s", e)
        return "(Policy text unavailable)"


def _save_audit_trail(claim_id: str, decision: dict) -> None:
    """Save the complete audit trail for a claim to a JSON file."""
    try:
        output_path = OUTPUTS_DIR / f"{claim_id}.json"
        with open(output_path, "w") as f:
            json.dump(decision, f, indent=2, default=str)
        logger.info("Audit trail saved to %s", output_path)
    except Exception as e:
        logger.warning("Failed to save audit trail for %s: %s", claim_id, e)


def _safe_float(val) -> float:
    """Safely convert a value to float."""
    try:
        if val is None:
            return 0.0
        return float(val)
    except (TypeError, ValueError):
        return 0.0
