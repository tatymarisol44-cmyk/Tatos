"""Deterministic ranking of real slots; preferences never create availability."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any, Literal

Preference = Literal["morning", "afternoon"]


def stated_preference(text: str) -> Preference | None:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )
    # Bare 'mañana' means tomorrow, not a preference for mornings. Ambiguous or
    # negative statements are left to clarification, never stored as a preference.
    if re.search(r"\b(no|nunca|tampoco)\b", text):
        return None
    morning = bool(
        re.search(
            r"\b(por la manana|en la manana|las mananas|mas temprano|prefiero temprano)\b", text
        )
    )
    afternoon = bool(
        re.search(r"\b(por la tarde|en la tarde|las tardes|mas tarde|prefiero tarde)\b", text)
    )
    if morning == afternoon:
        return None
    return "morning" if morning else "afternoon"


def historical_preference(visits: list[datetime]) -> Preference | None:
    """Newest first: at least two completed visits and a 2/3 weighted majority.

    Recent attendance outweighs older attendance; a tie is not a preference.
    """
    if len(visits) < 2:
        return None
    weights = list(range(len(visits), 0, -1))
    total = sum(weights)
    morning = sum(w for visit, w in zip(visits, weights, strict=True) if visit.hour < 12)
    if morning * 3 >= total * 2:
        return "morning"
    if (total - morning) * 3 >= total * 2:
        return "afternoon"
    return None


def rank_slots(slots: list[dict[str, Any]], preference: Preference | None) -> list[dict[str, Any]]:
    # A fresh sort per request adapts to the current agenda. Chronology breaks ties.
    def priority(slot: dict[str, Any]) -> tuple[bool, str]:
        morning = int(slot["local"][11:13]) < 12
        matches = preference is None or morning == (preference == "morning")
        return (not matches, slot["starts_at"])

    return sorted(slots, key=priority)[:3]


def numbered_options(slots: list[dict[str, Any]]) -> str:
    listing = "\n".join(f"{i}) {s['local']}" for i, s in enumerate(slots, 1))
    choices = "1" if len(slots) == 1 else "1 o 2" if len(slots) == 2 else "1, 2 o 3"
    return f"{listing}\nResponde {choices} para elegir. Comprobaremos el horario al confirmar."


def validated_offer(reply: str, slots: list[dict[str, Any]]) -> str:
    """Only the server's list is authoritative. A malformed offer uses fixed text."""
    # Keep only the known-safe greeting. Numbered times and the call to action
    # always come from code, even when the provider supplied a plausible list.
    first_line = reply.splitlines()[0].strip() if reply else ""
    greetings = {"¡Hola!", "¡Hola! Gracias por escribirnos.", "Gracias por escribirnos."}
    greeting = first_line if first_line in greetings else "¡Hola! Gracias por escribirnos."
    return greeting + " Estos son los horarios disponibles:\n" + numbered_options(slots)
