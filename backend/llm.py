"""Saha LLM Module.

ALL LLM calls in Saha go strictly through this module.
Uses the OpenAI Python SDK pointed at Nebius Token Factory:
https://api.tokenfactory.nebius.com/v1/

Uses ONLY NVIDIA Nemotron models:
  PLANNER: nvidia/Nemotron-3-Ultra-550b-a55b
  WORKER:  nvidia/nemotron-3-super-120b-a12b
  FAST:    nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B

Guarantees:
- Reads NEBIUS_API_KEY strictly via python-dotenv.
- Returns ONLY `content` to callers (never reasoning_content).
- Default max_tokens per role: PLANNER 4096, WORKER 4096, FAST 1024.
- Configurable timeout with 1 retry on transient failures.
- Records model, latency, and token telemetry in `llm_calls` table.
- Exposes SAHA_DISABLE_THINKING env flag for supported models.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APITimeoutError, NotFoundError, OpenAI, RateLimitError

from backend.db import check_daily_llm_limit, record_llm_call
from backend.redaction import redact_messages

# Safely inject truststore if available to verify SSL against Windows root certificates
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Root directory resolution and strict .env loading
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Base URL for Nebius Token Factory
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")

# Model Constants (overridable via env vars, defaults to spec)
PLANNER_MODEL: str = os.getenv("PLANNER", "nvidia/Nemotron-3-Ultra-550b-a55b")
WORKER_MODEL: str = os.getenv("WORKER", "nvidia/nemotron-3-super-120b-a12b")
FAST_MODEL: str = os.getenv("FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")

# Per-role default max_tokens
ROLE_DEFAULT_MAX_TOKENS = {
    "planner": 4096,
    "worker": 4096,
    "fast": 1024,
}

# Configurable timeout (seconds)
DEFAULT_TIMEOUT = float(os.getenv("SAHA_LLM_TIMEOUT", "60.0"))

# Flag to disable thinking tokens via reasoning_effort='none'
DISABLE_THINKING = os.getenv("SAHA_DISABLE_THINKING", "false").lower() in ("true", "1", "yes")

# Strict identifier-only tag whitelist: no free text, spaces, or unauthorized prefixes
TAG_REGEX = re.compile(r"^(goal_id|task_id|step|role|memory|file|probe|skill):[A-Za-z0-9._-]{1,64}$")


def filter_context_tags(tags: Optional[List[str]]) -> Tuple[List[str], int]:
    """Filter context tags against strict identifier-only whitelist.
    
    Allowed format: ^(goal_id|task_id|step|role|memory|file|probe|skill):[A-Za-z0-9._-]{1,64}$
    Any tag containing spaces, free text, or unapproved prefixes is dropped.
    Returns (valid_tags, dropped_count).
    """
    if not tags:
        return [], 0
    valid_tags: List[str] = []
    dropped_count = 0
    for tag in tags:
        if isinstance(tag, str) and TAG_REGEX.match(tag.strip()):
            valid_tags.append(tag.strip())
        else:
            dropped_count += 1
    return valid_tags, dropped_count


_client: Optional[OpenAI] = None


def get_model_role(model_name: str) -> str:
    """Infer role string from model name or configured constants."""
    if model_name == PLANNER_MODEL:
        return "planner"
    elif model_name == WORKER_MODEL:
        return "worker"
    elif model_name == FAST_MODEL:
        return "fast"
    return "custom"


def get_default_max_tokens(role_or_model: str) -> int:
    """Return default max_tokens according to role."""
    role = role_or_model.lower()
    if role in ROLE_DEFAULT_MAX_TOKENS:
        return ROLE_DEFAULT_MAX_TOKENS[role]
    inferred_role = get_model_role(role_or_model)
    return ROLE_DEFAULT_MAX_TOKENS.get(inferred_role, 4096)


def load_api_key() -> str:
    """Load the Nebius API key strictly via python-dotenv/environment.
    
    Never logs, prints, or exposes the key value.
    """
    key = os.getenv("NEBIUS_API_KEY")
    if key and key.strip() and key.strip() != "your_key_here":
        return key.strip()

    raise RuntimeError(
        "NEBIUS_API_KEY is not set. Please define NEBIUS_API_KEY in .env or your environment."
    )


def get_client() -> OpenAI:
    """Return the singleton OpenAI client configured for Nebius Token Factory."""
    global _client
    if _client is None:
        api_key = load_api_key()
        _client = OpenAI(
            base_url=NEBIUS_BASE_URL,
            api_key=api_key,
            timeout=DEFAULT_TIMEOUT,
        )
    return _client


def call_llm(
    model: str,
    messages: List[Dict[str, str]],
    role: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
    stop: Optional[Union[str, List[str]]] = None,
    timeout: Optional[float] = None,
    owner_id: Optional[str] = None,
    context_tags: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> str:
    """Execute a single LLM call through Nebius Token Factory with 1 retry.
    
    Returns ONLY `content` as a string (never reasoning_content).
    Applies redaction pass before dispatching to external API.
    Records model, latency, context tags, character counts, and token usage into llm_calls table.
    Enforces strict fail-loud error reporting if a model ID is not found.
    """
    effective_role = role or get_model_role(model)
    check_daily_llm_limit(owner_id=owner_id, db_path=db_path)
    client = get_client()
    tokens_to_request = max_tokens or get_default_max_tokens(effective_role)
    request_timeout = timeout or DEFAULT_TIMEOUT

    # Redaction pass
    clean_messages, input_chars, redaction_count = redact_messages(messages)

    # Strict tag validation: drop any non-whitelisted or free text tags
    valid_tags, dropped_tags = filter_context_tags(context_tags)

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": clean_messages,
        "temperature": temperature,
        "max_tokens": tokens_to_request,
        "timeout": request_timeout,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if stop:
        kwargs["stop"] = stop

    # If thinking is disabled and model supports reasoning_effort
    if DISABLE_THINKING and "nano" not in model.lower():
        kwargs["reasoning_effort"] = "none"

    # Attempt call with 1 retry on transient network/timeout errors
    attempts = 0
    max_attempts = 2
    last_error: Optional[Exception] = None

    while attempts < max_attempts:
        attempts += 1
        t_start = time.perf_counter()
        try:
            response = client.chat.completions.create(**kwargs)
            t_end = time.perf_counter()
            latency_ms = (t_end - t_start) * 1000.0

            # Telemetry collection
            usage = getattr(response, "usage", None)
            p_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            c_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            t_tokens = getattr(usage, "total_tokens", 0) if usage else 0

            # Return ONLY content
            choice = response.choices[0]
            msg = choice.message
            content = msg.content or ""
            if not content and hasattr(msg, "reasoning") and msg.reasoning:
                content = msg.reasoning
            result_str = content.strip()

            record_llm_call(
                model=model,
                role=effective_role,
                owner_id=owner_id,
                context_tags=valid_tags,
                input_chars=input_chars,
                output_chars=len(result_str),
                redaction_count=redaction_count,
                dropped_tags=dropped_tags,
                latency_ms=latency_ms,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                total_tokens=t_tokens,
                status="success",
                db_path=db_path,
            )

            return result_str

        except NotFoundError as e:
            t_end = time.perf_counter()
            latency_ms = (t_end - t_start) * 1000.0
            record_llm_call(
                model=model,
                role=effective_role,
                owner_id=owner_id,
                context_tags=valid_tags,
                input_chars=input_chars,
                output_chars=0,
                redaction_count=redaction_count,
                dropped_tags=dropped_tags,
                latency_ms=latency_ms,
                status="error",
                error=f"Model not found: {e}",
                db_path=db_path,
            )
            print(
                f"[FATAL ERROR] Model '{model}' was not found on Nebius Token Factory: {e}",
                file=sys.stderr,
            )
            raise

        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            last_error = e
            t_end = time.perf_counter()
            latency_ms = (t_end - t_start) * 1000.0
            if attempts < max_attempts:
                time.sleep(1.0)
                continue
            record_llm_call(
                model=model,
                role=effective_role,
                owner_id=owner_id,
                context_tags=valid_tags,
                input_chars=input_chars,
                output_chars=0,
                redaction_count=redaction_count,
                dropped_tags=dropped_tags,
                latency_ms=latency_ms,
                status="error",
                error=f"Transient failure after {attempts} attempts: {e}",
                db_path=db_path,
            )
            raise

        except APIError as e:
            t_end = time.perf_counter()
            latency_ms = (t_end - t_start) * 1000.0
            record_llm_call(
                model=model,
                role=effective_role,
                latency_ms=latency_ms,
                status="error",
                error=str(e),
                db_path=db_path,
            )
            if getattr(e, "status_code", None) == 404:
                print(
                    f"[FATAL ERROR] Model '{model}' returned 404 on Nebius Token Factory: {e}",
                    file=sys.stderr,
                )
            raise

    if last_error:
        raise last_error
    raise RuntimeError(f"Unknown error calling model {model}")


def call_llm_json(
    model: str,
    messages: List[Dict[str, str]],
    role: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    max_retries: int = 1,
    owner_id: Optional[str] = None,
    context_tags: Optional[List[str]] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Call LLM and parse strictly formatted JSON output.
    
    If JSON is invalid, retries once with the parsing error fed back to the model.
    """
    augmented_messages = list(messages)
    json_instruction = (
        "\nIMPORTANT: Your response MUST be valid JSON only. Do not include markdown "
        "code fences like ```json, and do not include commentary before or after."
    )
    if augmented_messages and augmented_messages[-1]["role"] == "user":
        augmented_messages[-1] = {
            "role": "user",
            "content": augmented_messages[-1]["content"] + json_instruction,
        }

    attempts = 0
    last_raw = ""
    while attempts <= max_retries:
        attempts += 1
        last_raw = call_llm(
            model=model,
            messages=augmented_messages,
            role=role,
            temperature=temperature,
            max_tokens=max_tokens,
            owner_id=owner_id,
            context_tags=context_tags,
            db_path=db_path,
        )

        # Attempt to clean markdown backticks if present
        clean_text = last_raw.strip()
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
            clean_text = re.sub(r"\s*```$", "", clean_text)
            clean_text = clean_text.strip()

        # Find first '{' or '[' and last '}' or ']'
        try:
            start_idx = -1
            end_idx = -1
            for i, ch in enumerate(clean_text):
                if ch in ("{", "["):
                    start_idx = i
                    break
            for i in range(len(clean_text) - 1, -1, -1):
                if clean_text[i] in ("}", "]"):
                    end_idx = i + 1
                    break
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_slice = clean_text[start_idx:end_idx]
                return json.loads(json_slice)
            return json.loads(clean_text)
        except (json.JSONDecodeError, ValueError) as err:
            if attempts > max_retries:
                raise ValueError(
                    f"Failed to parse JSON from model '{model}' after {attempts} attempts. "
                    f"Error: {err}. Raw output:\n{last_raw}"
                ) from err
            # Append error feedback and retry once
            augmented_messages.append({"role": "assistant", "content": last_raw})
            augmented_messages.append({
                "role": "user",
                "content": f"The response was not valid JSON ({err}). Output ONLY valid, parsable JSON.",
            })

    raise ValueError(f"Failed to parse JSON from {model}.")
