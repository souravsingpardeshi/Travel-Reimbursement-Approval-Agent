# Travel Reimbursement Approval Agent

A small agent that reads travel reimbursement claims and receipts, checks them against policy, and returns an approve/reject decision with an audit trail.

It uses deterministic Python tools for anything involving money or policy limits — the LLM (via Ollama) only writes the final explanation based on what the tools found. This keeps decisions consistent and auditable instead of relying on the model to "do math."

## Demo

**Upload screen**

<img width="1910" height="1031" alt="Screenshot 2026-07-01 at 12 40 04" src="https://github.com/user-attachments/assets/2c52b2e8-ff30-495a-9286-53beb62135d8" />


**Results page**

<img width="1908" height="981" alt="Screenshot 2026-07-01 at 12 40 43" src="https://github.com/user-attachments/assets/4d3773a9-569a-4e8a-890c-93c11808a0a1" />
<img width="1333" height="562" alt="Screenshot 2026-06-30 at 23 51 11" src="https://github.com/user-attachments/assets/4612c736-f4ba-4279-868d-c284f221a3de" />


## How it works

1. Upload an Excel file of claims + PDF receipts
2. For each claim, the agent runs 5 tools:
   - `policy_lookup` — pulls the relevant policy limits
   - `check_receipt_completeness` — checks the receipt exists and matches the claim
   - `check_limit` — compares claimed amount to the policy limit
   - `detect_duplicate` — flags repeat claims
   - `validate_output` — sanity-checks the final decision before it's saved
3. Tool results + policy text are passed to the LLM, which fills in a decision: Approve, Partially Approve, Reject, or Manual Review
4. The decision and full audit trail are saved to `outputs/<claim_id>.json`

If the LLM fails or returns something invalid, the claim goes to Manual Review instead of guessing.

## Setup

Requires Python 3.10+ and [Ollama](https://ollama.ai) running locally.

```bash
ollama pull llama3.2
ollama serve

pip install -r requirements.txt
python app.py
```

Open http://localhost:5001

## Try it with sample data

1. Upload `data/sample_claims.xlsx`
2. Upload the PDFs in `data/sample_receipts/`
3. Click "Run Agent"
4. Check the results table

Sample claims cover a few scenarios: a clean approval, an over-limit meal claim, a duplicate, a missing receipt, and a receipt date mismatch. Expected outcomes are in `data/sample_claims.xlsx` if you want to verify.

## Config

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.2` | Model to use |
| `LLM_TIMEOUT` | `120` | Timeout in seconds |
| `PORT` | `5000` | Flask port |
| `FLASK_DEBUG` | `1` | Flask debug mode |

## Project layout

```
app.py                     Flask routes: upload, run, results
agent/
  orchestrator.py          Main loop: runs tools, then calls the LLM
  llm_client.py             Ollama HTTP wrapper
  tools.py                  The 5 deterministic tools
  prompts.py                 Prompt templates
data/
  policy/                   Policy doc + limits.json
templates/                  Upload form + results page
static/style.css
outputs/                    Audit trail JSON per claim, auto-generated
```

## Notes / limitations

- Processes one Excel batch at a time, no streaming
- No database — results live in memory + JSON files, restarting clears the in-memory ones
- No login or access control (it's a demo)
- Receipts are matched to claims by filename (claim ID must appear in the PDF name)
- Runs on llama3.2 (3B) by default — a bigger model will give more reliable JSON output
- One currency per claim, no conversion
