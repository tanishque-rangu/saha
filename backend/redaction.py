"""Data redaction engine for Saha LLM prompts and context.

Redacts sensitive entities before sending prompts to external LLMs:
- Email addresses -> [EMAIL_REDACTED]
- Phone numbers -> [PHONE_REDACTED]
- API keys & secrets -> [KEY_REDACTED]
- Long digit sequences (16+ digits, e.g. credit cards) -> [DIGITS_REDACTED]

Configurable via SAHA_REDACT_ENABLED environment variable (default: true).
"""

import os
import re
from typing import Any, Dict, List, Tuple

# Pre-compiled regex patterns
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

PHONE_PATTERN = re.compile(
    r"(?:(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b)|(?:\+\d{1,3}\s\d{10}\b)",
)

API_KEY_PATTERN = re.compile(
    r"(?i)\b(?:Bearer\s+|api[_-]?key\s*[:=]\s*)[A-Za-z0-9_\-\.]{16,}\b|"
    r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]{32,})\b"
)

LONG_DIGITS_PATTERN = re.compile(
    r"\b(?:\d{4}[-\s]?){3}\d{4}\b|\b\d{16,}\b"
)


def is_redaction_enabled() -> bool:
    """Return whether prompt redaction is globally active."""
    return os.getenv("SAHA_REDACT_ENABLED", "true").lower() in ("true", "1", "yes")


def redact_text(text: str) -> Tuple[str, int]:
    """Redact sensitive patterns in text and return (redacted_text, total_redactions)."""
    if not text or not is_redaction_enabled():
        return text, 0

    total_redacted = 0

    # 1. API Keys & Secrets
    def _sub_key(match):
        nonlocal total_redacted
        total_redacted += 1
        return "[KEY_REDACTED]"

    text = API_KEY_PATTERN.sub(_sub_key, text)

    # 2. Emails
    def _sub_email(match):
        nonlocal total_redacted
        total_redacted += 1
        return "[EMAIL_REDACTED]"

    text = EMAIL_PATTERN.sub(_sub_email, text)

    # 3. Long digit sequences (credit cards, IDs)
    def _sub_digits(match):
        nonlocal total_redacted
        total_redacted += 1
        return "[DIGITS_REDACTED]"

    text = LONG_DIGITS_PATTERN.sub(_sub_digits, text)

    # 4. Phone numbers
    def _sub_phone(match):
        nonlocal total_redacted
        total_redacted += 1
        return "[PHONE_REDACTED]"

    text = PHONE_PATTERN.sub(_sub_phone, text)

    return text, total_redacted


def redact_messages(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Redact a list of OpenAI chat messages.
    
    Returns:
        (redacted_messages, input_chars, total_redaction_count)
    """
    redacted = []
    total_redactions = 0
    total_chars = 0

    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            clean_content, count = redact_text(content)
            total_redactions += count
            total_chars += len(clean_content)
            new_msg = dict(m)
            new_msg["content"] = clean_content
            redacted.append(new_msg)
        else:
            redacted.append(m)

    return redacted, total_chars, total_redactions
