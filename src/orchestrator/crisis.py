"""Words that may signal risk to life, and requests for a person, in incoming messages.

This is a router, not an assessment: it decides who must look at a message, never what it
means clinically. It is tuned for recall (a false alarm costs a professional one look; a
miss can cost much more), matches accent-folded Spanish and English phrases, and its output
only ever goes to a person: the AI never answers a message flagged here (ADR 0014). The
list is a starting point to review with the practice's professionals (DECISIONS-PENDING P4)."""

from __future__ import annotations

import re
from typing import Literal

from orchestrator.campaigns import is_stop
from orchestrator.risk import fold

Intent = Literal["stop", "crisis", "human", "other"]

_CRISIS = re.compile(
    r"\b("
    r"suicid\w*|"
    r"(me )?quiero morir|quisiera morir(me)?|ganas de morir|"
    r"no quiero (seguir )?vivir|no vale la pena vivir|"
    r"quitarme la vida|matarme|me voy a matar|acabar con mi vida|acabar con todo|"
    r"hacerme dano|lastimarme|autolesion\w*|cortarme|"
    r"desaparecer para siempre|ya no puedo mas|"
    r"kill myself|want to die|end my life|self[- ]harm|hurt myself"
    r")\b"
)
_HUMAN = re.compile(
    r"\b("
    r"(hablar|chatear|comunicarme) con (una )?(persona|humano|alguien|la doctora?|el doctor|"
    r"el psicologo|la psicologa|el psiquiatra|la psiquiatra|un asesor|una asesora)|"
    r"persona real|un humano|agente humano|human agent|real person|talk to (a )?(person|human)"
    r")\b"
)


def classify(text: str) -> Intent:
    """Crisis first: a message that says STOP and also signals risk still reaches a person."""
    folded = fold(text)
    if _CRISIS.search(folded):
        return "crisis"
    if is_stop(text):
        return "stop"
    if _HUMAN.search(folded):
        return "human"
    return "other"
