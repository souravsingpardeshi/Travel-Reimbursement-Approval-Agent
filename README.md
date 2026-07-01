# Travel Reimbursement Approval Agent

A lightweight, demo-ready GenAI agent that reads travel reimbursement claims and supporting receipts, grounds its decisions in policy rules, calls deterministic tools to verify facts, and returns structured decisions.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Flask Web App                               │
│  ┌──────────┐    ┌──────────┐    ┌──────────────┐                  │
│  │  Upload   │───▶│  /run    │───▶│  /results    │                  │
│  │  Form     │    │  (POST)  │    │  Decision    │                  │
│  │  (index)  │    │          │    │  Table       │                  │
│  └──────────┘    └────┬─────┘    └──────────────┘                  │
│                       │                                             │
└───────────────────────┼─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    orchestrator.py                                   │
│                                                                     │
│  For each claim:                                                    │
│  ┌──────────────────────────────────────────────────┐               │
│  │ 1. Run Deterministic Tools                       │               │
│  │    ┌─────────────┐  ┌──────────┐  ┌──────────┐  │               │
│  │    │ policy_     │  │ check_   │  │ check_   │  │               │
│  │    │ lookup      │  │ receipt  │  │ limit    │  │               │
│  │    └─────────────┘  └──────────┘  └──────────┘  │               │
│  │    ┌─────────────┐                               │               │
│  │    │ detect_     │                               │               │
│  │    │ duplicate   │                               │               │
│  │    └─────────────┘                               │               │
│  ├──────────────────────────────────────────────────┤               │
│  │ 2. Build LLM Prompt (tool results + policy)     │               │
│  ├──────────────────────────────────────────────────┤               │
│  │ 3. Call Ollama LLM (JSON mode)                   │──▶ Ollama    │
│  ├──────────────────────────────────────────────────┤   (llama3.2) │
│  │ 4. Validate Output (schema + sanity checks)     │               │
│  ├──────────────────────────────────────────────────┤               │
│  │ 5. Save Audit Trail (outputs/<claim_id>.json)    │               │
│  └──────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) installed and running locally
- `llama3.2` model pulled

```bash
# Install and start Ollama (if not already running)
ollama pull llama3.2
ollama serve  # runs on http://localhost:11434 by default
```

### Setup

```bash
# Clone and install dependencies
cd Travel-Reimbursement-Approval-Agent
pip install -r requirements.txt

# Generate sample data (Excel claims + PDF receipts)
python generate_sample_data.py

# Run the Flask app
python app.py
```

Open http://localhost:5000 in your browser.

### Demo Walkthrough

1. Go to http://localhost:5000
2. Upload `data/sample_claims.xlsx` as the claims file
3. Upload all PDFs from `data/sample_receipts/` as receipts
4. Click **"Run Agent"**
5. View results on the `/results` page with color-coded decisions

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `LLM_TIMEOUT` | `120` | LLM request timeout (seconds) |
| `PORT` | `5000` | Flask server port |
| `FLASK_DEBUG` | `1` | Enable Flask debug mode |

## Project Structure

```
travel-reimbursement-agent/
├── app.py                     # Flask app: routes for upload, run, results
├── agent/
│   ├── orchestrator.py        # Core agentic loop (explicit, no black-box chains)
│   ├── llm_client.py          # Thin Ollama HTTP wrapper (chat + JSON mode)
│   ├── tools.py               # 5 deterministic tool implementations
│   └── prompts.py             # System prompt + decision prompt templates
├── data/
│   ├── policy/
│   │   ├── travel_policy.md   # Mock policy document (human-readable)
│   │   └── limits.json        # Per-category limits lookup table
│   ├── sample_claims.xlsx     # 6 sample claims (5 unique + 1 duplicate)
│   └── sample_receipts/       # 4 mock PDF receipts (1 deliberately missing)
├── templates/
│   ├── index.html             # Upload form with Ollama status indicator
│   └── results.html           # Decision table with expandable audit trail
├── static/style.css           # Dark-themed CSS
├── outputs/                   # Auto-generated: audit trail JSONs per claim
├── tests/
│   └── test_agent.py          # Tool-only + full agent evaluation script
├── generate_sample_data.py    # One-time script to create sample Excel/PDFs
├── requirements.txt
└── README.md
```

## Tools

All tools are deterministic Python functions in `agent/tools.py`. The LLM never calculates monetary amounts — all comparisons come from tools.

| Tool | Purpose |
|---|---|
| `policy_lookup(category, currency, city)` | Returns policy limits + rules for a category |
| `check_receipt_completeness(claim_id, amount, currency)` | Checks PDF receipt presence, extracts vendor/amount/date, flags mismatches |
| `check_limit(category, amount, currency, city)` | Compares amount against policy limit, returns excess/approved amounts |
| `detect_duplicate(claim_id, all_claims)` | Flags claims with same employee+date+category+amount |
| `validate_output(decision_json)` | Schema/sanity validator (amounts, decision values, confidence range) |

## Sample Claims & Expected Decisions

| Claim ID | Category | Amount | Scenario | Expected Decision |
|---|---|---|---|---|
| CLM001 | Hotel | ₹4,500 | Clean claim, within limits, receipt matches | **Approve** |
| CLM002 | Meals | ₹2,800 | Over daily limit (₹1,500) | **Partially Approve** |
| CLM003 | Local Transport | ₹1,200 | Duplicate of CLM003B | **Reject** |
| CLM003B | Local Transport | ₹1,200 | Duplicate of CLM003 | **Reject** |
| CLM004 | Airfare | ₹12,500 | No receipt uploaded | **Manual Review** |
| CLM005 | Hotel | ₹4,800 | Receipt date mismatch (claim: Jun 20, receipt: Jun 22) | **Manual Review** |

## Decision Schema

Every claim returns this JSON structure:

```json
{
  "claim_id": "CLM001",
  "decision": "Approve | Partially Approve | Reject | Manual Review",
  "claimed_amount": 4500.0,
  "approved_amount": 4500.0,
  "deducted_amount": 0.0,
  "missing_documents": [],
  "policy_reference": ["Section 3: Per-Category Limits (domestic)"],
  "confidence": 0.95,
  "explanation": "All checks passed. Amount within hotel daily limit.",
  "tools_used": ["policy_lookup", "check_receipt_completeness", "check_limit", "detect_duplicate", "validate_output"]
}
```

## Testing

```bash
# Run tool-only tests (no Ollama needed)
python -m tests.test_agent --tools-only

# Run full agent tests (requires Ollama with llama3.2)
python -m tests.test_agent
```

## Key Design Decisions

1. **Tools-first, LLM-second**: All monetary comparisons and policy lookups happen in deterministic Python tools. The LLM only reasons over tool outputs — it never invents numbers.

2. **Fail-safe to Manual Review**: If the LLM is unavailable, returns invalid JSON, or fails validation, the decision defaults to "Manual Review" with an explanation. The system never crashes or silently approves.

3. **No LangChain**: The Ollama integration is a thin HTTP wrapper (~100 lines). The agentic loop in `orchestrator.py` is explicit and easy to read/demo.

4. **Audit trail**: Every tool call (inputs + outputs) and LLM call (raw response) is logged to both console and `outputs/<claim_id>.json` for full transparency.

5. **JSON-mode + validation**: Ollama's `format: "json"` is used for structured output, plus a secondary `validate_output` tool as a safety net.

## Assumptions & Limitations

- **Single-batch processing**: All claims in the Excel file are processed as one batch (no incremental/streaming).
- **No database**: Results are stored in memory and as JSON files. Restarting the app clears in-memory results (JSON files persist).
- **No authentication**: This is a demo app — no login or role-based access control.
- **Receipt matching by filename**: Receipts are matched to claims by checking if the claim_id appears in the PDF filename.
- **LLM quality**: Decision quality depends on the local LLM model. `llama3.2` (3B parameters) may produce less reliable JSON than larger models.
- **Single currency per claim**: Each claim has one currency. Cross-currency conversion is not supported.

## License

MIT — Demo/educational use.