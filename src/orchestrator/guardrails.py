"""Input/output guardrails: size limits, prompt-injection heuristics, PII redaction.

These are cheap deterministic first-line checks. They complement, not replace,
provider-side safety and an LLM-based classifier for high-risk deployments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?|rules)",
    r"disregard (all |any )?(the )?(previous|prior|above|system)",
    r"(reveal|print|show|repeat) (me )?(your|the) (system|hidden) (prompt|instructions)",
    r"you are now (in )?(dan|developer mode|jailbreak)",
    r"ignora (todas |las )*(instrucciones|reglas) (anteriores|previas)",
    r"(muestra|revela|repite) (tu|el) (prompt|mensaje) (de sistema|del sistema|oculto)",
]
_INJECTION = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\w)\+?\d{1,3}?[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")


@dataclass
class GuardrailResult:
    allowed: bool
    text: str
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        checksum += d
    return len(digits) >= 13 and checksum % 10 == 0


def redact_pii(text: str) -> tuple[str, list[str]]:
    found: list[str] = []

    def sub(pattern: re.Pattern[str], label: str, value: str, check: bool = True) -> str:
        def repl(m: re.Match[str]) -> str:
            if check and label == "credit_card" and not _luhn_ok(m.group(0)):
                return m.group(0)
            found.append(label)
            return f"[REDACTED_{label.upper()}]"

        return pattern.sub(repl, value)

    text = sub(_EMAIL, "email", text)
    text = sub(_SSN, "ssn", text)
    text = sub(_CARD, "credit_card", text)
    text = sub(_PHONE, "phone", text)
    return text, sorted(set(found))


def check_input(
    text: str, *, max_chars: int, injection_action: str = "block", redact: bool = True
) -> GuardrailResult:
    stripped = text.strip()
    if not stripped:
        return GuardrailResult(False, text, ["empty_input"])
    if len(stripped) > max_chars:
        return GuardrailResult(False, text, [f"input_too_long:{len(stripped)}>{max_chars}"])
    result = GuardrailResult(True, stripped)
    if _INJECTION.search(stripped):
        if injection_action == "block":
            return GuardrailResult(False, text, ["prompt_injection"])
        result.flags.append("prompt_injection")
    if redact:
        result.text, pii = redact_pii(result.text)
        result.flags.extend(f"pii:{p}" for p in pii)
    return result


def check_output(text: str, *, redact: bool = True) -> GuardrailResult:
    result = GuardrailResult(True, text)
    if redact:
        result.text, pii = redact_pii(text)
        result.flags.extend(f"pii:{p}" for p in pii)
    return result
