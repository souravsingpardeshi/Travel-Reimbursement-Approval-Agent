"""
llm_client.py — Thin wrapper around the Ollama HTTP API.

Handles:
  - Chat completions via POST /api/chat
  - JSON-mode prompting (format: "json")
  - Timeout + single-retry logic
  - Structured logging of every LLM call
"""

import json
import logging
import os
import time
import requests
from pathlib import Path
from dotenv import load_dotenv
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    logger.info(f"Loaded configuration settings from {ENV_PATH}")
else:
    logger.warning(f".env file not found at {ENV_PATH}. Falling back to system defaults.")

# Extract variables with robust default values
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

try:
    LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))
except ValueError:
    logger.warning("Invalid LLM_TIMEOUT value found in environment. Defaulting to 120 seconds.")
    LLM_TIMEOUT = 120


def chat(system_prompt: str, user_prompt: str, json_mode: bool = True) -> dict:
    """
    Send a chat completion request to Ollama and return the parsed response.

    Args:
        system_prompt: The system-level instruction for the model.
        user_prompt: The user-level content (claim data + tool results).
        json_mode: If True, request JSON-formatted output from Ollama.

    Returns:
        dict with keys:
            - "content": the raw text response from the LLM
            - "parsed_json": parsed JSON dict if json_mode and parsing succeeded, else None
            - "model": model name used
            - "duration_ms": round-trip time in milliseconds
            - "error": error string if the call failed, else None
    """
    url = f"{OLLAMA_HOST}/api/chat"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,  # Low temp for deterministic decisions
            "num_predict": 2048,
        },
    }

    if json_mode:
        payload["format"] = "json"

    result = {
        "content": None,
        "parsed_json": None,
        "model": OLLAMA_MODEL,
        "duration_ms": 0,
        "error": None,
    }

    for attempt in range(2):  # Try once, retry once on failure
        try:
            logger.info(
                "LLM call attempt %d/%d to %s (model=%s, json_mode=%s)",
                attempt + 1, 2, url, OLLAMA_MODEL, json_mode,
            )
            start = time.time()
            resp = requests.post(url, json=payload, timeout=LLM_TIMEOUT)
            elapsed_ms = int((time.time() - start) * 1000)
            result["duration_ms"] = elapsed_ms

            resp.raise_for_status()
            body = resp.json()

            content = body.get("message", {}).get("content", "")
            result["content"] = content
            logger.info("LLM responded in %d ms (%d chars)", elapsed_ms, len(content))

            if json_mode:
                try:
                    parsed = json.loads(content)
                    result["parsed_json"] = parsed
                except json.JSONDecodeError as e:
                    logger.warning("LLM returned invalid JSON: %s", e)
                    # Try to extract JSON from the response
                    parsed = _try_extract_json(content)
                    if parsed:
                        result["parsed_json"] = parsed
                    else:
                        result["error"] = f"Invalid JSON from LLM: {e}"

            return result

        except requests.exceptions.Timeout:
            logger.warning("LLM call timed out (attempt %d)", attempt + 1)
            result["error"] = f"LLM request timed out after {LLM_TIMEOUT}s"
        except requests.exceptions.ConnectionError:
            logger.warning("Cannot connect to Ollama at %s (attempt %d)", OLLAMA_HOST, attempt + 1)
            result["error"] = f"Cannot connect to Ollama at {OLLAMA_HOST}"
        except requests.exceptions.HTTPError as e:
            logger.warning("LLM HTTP error: %s (attempt %d)", e, attempt + 1)
            result["error"] = f"LLM HTTP error: {e}"
        except Exception as e:
            logger.warning("Unexpected LLM error: %s (attempt %d)", e, attempt + 1)
            result["error"] = f"Unexpected error: {e}"

        if attempt == 0:
            logger.info("Retrying LLM call in 2 seconds...")
            time.sleep(2)

    logger.error("LLM call failed after 2 attempts: %s", result["error"])
    return result


def _try_extract_json(text: str) -> dict | None:
    """
    Attempt to extract a JSON object from text that may contain
    markdown code fences or surrounding prose.
    """
    import re

    # Try to find JSON within code fences
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find a bare JSON object
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def check_ollama_status() -> dict:
    """
    Quick health check — verify Ollama is reachable and the model is available.

    Returns:
        dict with "available" (bool), "models" (list), "error" (str|None)
    """
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        model_available = any(OLLAMA_MODEL in m for m in models)
        return {
            "available": model_available,
            "models": models,
            "error": None if model_available else f"Model '{OLLAMA_MODEL}' not found. Available: {models}",
        }
    except Exception as e:
        return {"available": False, "models": [], "error": str(e)}
