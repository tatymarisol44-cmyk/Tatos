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


def detect_injection(text: str) -> bool:
    """Heuristic prompt-injection check, for user input and ingested documents alike."""
    return bool(_INJECTION.search(text))


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        checksum += d
    return len(digits) >= 13 and checksum % 10 == 0


_CEDULA = re.compile(r"(?<![\d-])\d{10}(?![\d-])")


def cedula_ok(number: str) -> bool:
    """Ecuadorian national id (cédula) of a natural person: province 01-24 or 30, third
    digit below 6, and the modulo-10 check digit. A mobile number (09 + 6..9) fails the
    third-digit rule, so phone numbers are not mistaken for ids."""
    if len(number) != 10 or not number.isdigit():
        return False
    province, third = int(number[:2]), int(number[2])
    if not (1 <= province <= 24 or province == 30) or third >= 6:
        return False
    total = 0
    for i, digit in enumerate(number[:9]):
        product = int(digit) * (2 if i % 2 == 0 else 1)
        total += product - 9 if product > 9 else product
    return (10 - total % 10) % 10 == int(number[9])


def person_identifiers(text: str) -> list[str]:
    """Kinds of national or payment identifiers of a PERSON found in `text` (cédula, US
    SSN, a Luhn-valid card number). Contact data is not listed: a business's own phone and
    e-mail belong in its FAQ."""
    found = set()
    if any(cedula_ok(m.group(0)) for m in _CEDULA.finditer(text)):
        found.add("cedula")
    if _SSN.search(text):
        found.add("ssn")
    if any(_luhn_ok(m.group(0)) for m in _CARD.finditer(text)):
        found.add("credit_card")
    return sorted(found)


def redact_pii(text: str) -> tuple[str, list[str]]:
    found: list[str] = []

    def sub(pattern: re.Pattern[str], label: str, value: str, check: bool = True) -> str:
        def repl(m: re.Match[str]) -> str:
            if check and label == "credit_card" and not _luhn_ok(m.group(0)):
                return m.group(0)
            if check and label == "cedula" and not cedula_ok(m.group(0)):
                return m.group(0)
            found.append(label)
            return f"[REDACTED_{label.upper()}]"

        return pattern.sub(repl, value)

    text = sub(_EMAIL, "email", text)
    text = sub(_SSN, "ssn", text)
    text = sub(_CEDULA, "cedula", text)
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
    if detect_injection(stripped):
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
