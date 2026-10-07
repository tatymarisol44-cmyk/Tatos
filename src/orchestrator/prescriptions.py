"""Worksheet for the ACESS special prescription (narcotic and psychotropic medicines).

The legal document is the numbered ACESS form (Resolución ACESS-2022-0046, Art. 4); this
system never issues it. The doctor fills the worksheet, this module checks it against the
fields of Art. 6 and the writing rules of Art. 27, and the doctor copies it (digital filling
of the original and the copy is valid, Art. 27) onto the form whose number is recorded.

What this module does NOT do: choose a drug, a dose or a duration; verify that the person
exists; or check that a CIE-10 code is a real entry (only its shape). The AI never fills the
worksheet. Every finding names the article it comes from; checks that rest on something not
read in the primary text are warnings, never errors."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# Art. 25: only médicos (general and specialist) and odontólogos.
PRESCRIBERS = ("medico_general", "medico_especialista", "odontologo")
# ICD-10 shape: a letter, two digits, optionally a dot and one or two characters (F32.1).
CIE10 = re.compile(r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,2})?$")
# Common prescription shorthand that Art. 27 forbids outside the concentration field
# ("sin siglas ni abreviaturas"; Art. 6 II(ii) allows accepted abbreviations only there).
# Heuristic list, matched as whole words on accent-folded, lower-cased text.
SHORTHAND = re.compile(
    r"(?<![\w/])(bid|tid|qid|qd|qhs|prn|sos|vo|v\.o\.|im|iv|sc|sl|hs|c/\d+\s*h?|cada\s*\d+h|stat)(?![\w/])"
)
# Search-result summary only, not read in the articles: maximum days per special
# prescription. A warning, never an error, until the article is confirmed (lawyer's F2).
MAX_DAYS_TO_VERIFY = 90


class SpecialPrescription(BaseModel):
    """The fields of ACESS-2022-0046 Art. 6, as the doctor writes them."""

    # Header
    acess_number: str = Field(default="", max_length=40)
    substance_type: Literal["estupefaciente", "psicotropico"] | None = None
    # I. General data
    city: str = Field(default="", max_length=80)
    date: str = Field(default="", max_length=10)  # dd/mm/aaaa
    patient_full_name: str = Field(default="", max_length=160)
    patient_age_years: int | None = Field(default=None, ge=0, le=130)
    id_type: Literal["cedula", "pasaporte"] = "cedula"
    id_number: str = Field(default="", max_length=20)
    clinical_record_number: str = Field(default="", max_length=40)
    diagnosis_cie10: str = Field(default="", max_length=8)
    establishment: str = Field(default="", max_length=160)
    # II. Medicine
    generic_name: str = Field(default="", max_length=120)
    concentration: str = Field(default="", max_length=60)
    pharmaceutical_form: str = Field(default="", max_length=60)
    quantity_number: int | None = Field(default=None, ge=1, le=9999)
    quantity_words: str = Field(default="", max_length=120)
    dose: str = Field(default="", max_length=80)
    frequency: str = Field(default="", max_length=80)
    route: str = Field(default="", max_length=60)
    treatment_days: int | None = Field(default=None, ge=1, le=3650)
    # III. Prescriber
    prescriber_full_name: str = Field(default="", max_length=160)
    prescriber_profession: str = Field(default="", max_length=40)
    prescriber_phone: str = Field(default="", max_length=20)
    acess_title_registry: str = Field(default="", max_length=40)


@dataclass(frozen=True)
class Finding:
    field: str
    code: str
    ref: str  # article it comes from


@dataclass
class WorksheetCheck:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_REQUIRED: dict[str, str] = {
    "acess_number": "Art. 6 a) header i",
    "substance_type": "Art. 6 a) header iv",
    "city": "Art. 6 I i",
    "date": "Art. 6 I i",
    "patient_full_name": "Art. 6 I ii",
    "patient_age_years": "Art. 6 I iii",
    "id_number": "Art. 6 I iv",
    "clinical_record_number": "Art. 6 I v",
    "diagnosis_cie10": "Art. 6 I vi",
    "establishment": "Art. 6 I vii",
    "generic_name": "Art. 6 II i",
    "concentration": "Art. 6 II ii",
    "pharmaceutical_form": "Art. 6 II iii",
    "quantity_number": "Art. 6 II iv",
    "quantity_words": "Art. 6 II iv",
    "dose": "Art. 6 II v",
    "frequency": "Art. 6 II vi",
    "route": "Art. 6 II vii",
    "treatment_days": "Art. 6 II viii",
    "prescriber_full_name": "Art. 6 III i",
    "prescriber_profession": "Art. 6 III ii",
    "prescriber_phone": "Art. 6 III ii",
    "acess_title_registry": "Art. 6 III iii",
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# --- Spanish number words (for "cantidad en letras y números", Art. 6 II iv) ------------

_UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21, "veintiuno": 21,
    "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29,
}  # fmt: skip
_TENS = {
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
    "ochenta": 80, "noventa": 90,
}  # fmt: skip
_HUNDREDS = {
    "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300,
    "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500,
    "quinientas": 500, "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
    "setecientas": 700, "ochocientos": 800, "ochocientas": 800, "novecientos": 900,
    "novecientas": 900,
}  # fmt: skip


def words_to_int(text: str) -> int | None:
    """Spanish cardinal words up to 9999 ("treinta y dos", "ciento veinte"); None if the
    text is not a number written in words."""
    words = [w for w in re.split(r"[\s-]+", _fold(text).strip()) if w and w != "y"]
    if not words:
        return None
    total = current = 0
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word in _HUNDREDS:
            current += _HUNDREDS[word]
        elif word == "mil":
            total += (current or 1) * 1000
            current = 0
        else:
            return None
    return total + current


# --- Ecuadorian cédula (format check only, not an identity check) ----------------------


def cedula_shape_ok(number: str) -> bool:
    """The public modulo-10 check of a 10-digit Ecuadorian cédula: province 01-24 or 30,
    third digit below 6, check digit. It says the number is well formed, not that it
    belongs to the patient."""
    if not re.fullmatch(r"\d{10}", number):
        return False
    province, third = int(number[:2]), int(number[2])
    if not (1 <= province <= 24 or province == 30) or third >= 6:
        return False
    total = 0
    for i, digit in enumerate(number[:9]):
        product = int(digit) * (2 if i % 2 == 0 else 1)
        total += product - 9 if product > 9 else product
    return (10 - total % 10) % 10 == int(number[9])


def _date_ok(value: str, today: date) -> bool:
    try:
        parsed = datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return False
    return parsed <= today


def check_worksheet(rx: SpecialPrescription, *, today: date | None = None) -> WorksheetCheck:
    today = today or date.today()
    check = WorksheetCheck()
    for name, ref in _REQUIRED.items():
        value = getattr(rx, name)
        if value is None or (isinstance(value, str) and not value.strip()):
            check.errors.append(Finding(name, "missing", f"ACESS-2022-0046 {ref}"))
    missing = {f.field for f in check.errors}

    if "date" not in missing and not _date_ok(rx.date, today):
        check.errors.append(
            Finding("date", "not_dd_mm_aaaa_or_future", "ACESS-2022-0046 Art. 6 I i")
        )
    if "id_number" not in missing and rx.id_type == "cedula" and not cedula_shape_ok(rx.id_number):
        check.errors.append(
            Finding("id_number", "cedula_not_well_formed", "ACESS-2022-0046 Art. 6 I iv")
        )
    if "diagnosis_cie10" not in missing and not CIE10.fullmatch(rx.diagnosis_cie10.strip().upper()):
        check.errors.append(
            Finding("diagnosis_cie10", "not_cie10_shape", "ACESS-2022-0046 Art. 6 I vi")
        )
    if "prescriber_profession" not in missing and rx.prescriber_profession not in PRESCRIBERS:
        check.errors.append(
            Finding("prescriber_profession", "not_a_prescriber", "ACESS-2022-0046 Art. 25")
        )
    quantity_given = not ({"quantity_number", "quantity_words"} & missing)
    if quantity_given and words_to_int(rx.quantity_words) != rx.quantity_number:
        check.errors.append(
            Finding("quantity_words", "words_do_not_match_number", "ACESS-2022-0046 Art. 6 II iv")
        )
    for name in ("dose", "frequency", "route", "generic_name", "pharmaceutical_form"):
        if name not in missing and SHORTHAND.search(_fold(getattr(rx, name))):
            check.errors.append(Finding(name, "abbreviation", "ACESS-2022-0046 Art. 27"))
    if rx.treatment_days is not None and rx.treatment_days > MAX_DAYS_TO_VERIFY:
        check.warnings.append(
            Finding(
                "treatment_days", "over_90_days_to_verify", "unconfirmed (lawyer's question F2)"
            )
        )
    return check
