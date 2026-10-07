"""The ACESS special-prescription worksheet (ACESS-2022-0046 Arts. 6, 25, 27). Synthetic
data only: the cédula below is computed to be well formed, not taken from anyone."""

from __future__ import annotations

from datetime import date

import pytest

from orchestrator.prescriptions import (
    SpecialPrescription,
    cedula_shape_ok,
    check_worksheet,
    words_to_int,
)

TODAY = date(2026, 10, 7)


def synthetic_cedula(first_nine: str = "171003406") -> str:
    total = 0
    for i, digit in enumerate(first_nine):
        product = int(digit) * (2 if i % 2 == 0 else 1)
        total += product - 9 if product > 9 else product
    return first_nine + str((10 - total % 10) % 10)


def worksheet(**overrides: object) -> SpecialPrescription:
    data: dict[str, object] = {
        "acess_number": "000123",
        "substance_type": "psicotropico",
        "city": "Quito",
        "date": "06/10/2026",
        "patient_full_name": "Paciente Sintético Demo",
        "patient_age_years": 34,
        "id_type": "cedula",
        "id_number": synthetic_cedula(),
        "clinical_record_number": "HC-0001",
        "diagnosis_cie10": "F41.1",
        "establishment": "Consultorio Demo",
        "generic_name": "Sertralina",
        "concentration": "50 mg",
        "pharmaceutical_form": "Tableta",
        "quantity_number": 30,
        "quantity_words": "treinta",
        "dose": "Una tableta",
        "frequency": "Una vez al día por la mañana",
        "route": "Oral",
        "treatment_days": 30,
        "prescriber_full_name": "Médica Demo",
        "prescriber_profession": "medico_especialista",
        "prescriber_phone": "022000000",
        "acess_title_registry": "REG-0001",
        **overrides,
    }
    return SpecialPrescription.model_validate(data)


def codes(rx: SpecialPrescription) -> set[tuple[str, str]]:
    return {(f.field, f.code) for f in check_worksheet(rx, today=TODAY).errors}


def test_a_complete_worksheet_passes() -> None:
    result = check_worksheet(worksheet(), today=TODAY)
    assert result.ok and result.warnings == []


def test_every_article_6_field_is_required_and_cites_its_article() -> None:
    empty = SpecialPrescription()
    result = check_worksheet(empty, today=TODAY)
    missing = {f.field for f in result.errors if f.code == "missing"}
    assert {"acess_number", "diagnosis_cie10", "quantity_words", "acess_title_registry"} <= missing
    assert len(missing) == 23
    assert all(f.ref.startswith("ACESS-2022-0046 Art. 6") for f in result.errors)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"prescriber_profession": "psicologo"}, ("prescriber_profession", "not_a_prescriber")),
        ({"diagnosis_cie10": "F4"}, ("diagnosis_cie10", "not_cie10_shape")),
        ({"diagnosis_cie10": "ansiedad"}, ("diagnosis_cie10", "not_cie10_shape")),
        ({"quantity_words": "veinte"}, ("quantity_words", "words_do_not_match_number")),
        ({"quantity_words": "30"}, ("quantity_words", "words_do_not_match_number")),
        ({"date": "2026-10-06"}, ("date", "not_dd_mm_aaaa_or_future")),
        ({"date": "08/10/2026"}, ("date", "not_dd_mm_aaaa_or_future")),  # tomorrow
        ({"id_number": "1710034060"}, ("id_number", "cedula_not_well_formed")),
        ({"frequency": "1 tab BID"}, ("frequency", "abbreviation")),
        ({"frequency": "c/8h"}, ("frequency", "abbreviation")),
        ({"route": "VO"}, ("route", "abbreviation")),
    ],
)
def test_each_rule_is_enforced(override: dict[str, object], expected: tuple[str, str]) -> None:
    assert expected in codes(worksheet(**override))


def test_abbreviations_are_allowed_in_the_concentration() -> None:
    # Art. 6 II(ii): internationally accepted abbreviations are allowed there.
    assert check_worksheet(worksheet(concentration="50 mg/ml"), today=TODAY).ok


def test_passports_skip_the_cedula_check() -> None:
    assert check_worksheet(worksheet(id_type="pasaporte", id_number="X1234567"), today=TODAY).ok


def test_the_unconfirmed_90_day_limit_is_only_a_warning() -> None:
    result = check_worksheet(worksheet(treatment_days=120), today=TODAY)
    assert result.ok
    assert [w.code for w in result.warnings] == ["over_90_days_to_verify"]


@pytest.mark.parametrize(
    ("words", "number"),
    [
        ("treinta", 30),
        ("treinta y dos", 32),
        ("Veintiún", 21),
        ("ciento veinte", 120),
        ("cien", 100),
        ("doscientas cincuenta", 250),
        ("mil quinientos", 1500),
        ("dos mil", 2000),
        ("treinta tabletas", None),
        ("", None),
    ],
)
def test_spanish_number_words(words: str, number: int | None) -> None:
    assert words_to_int(words) == number


def test_cedula_shape() -> None:
    assert cedula_shape_ok(synthetic_cedula())
    assert not cedula_shape_ok("123")
    assert not cedula_shape_ok("9910034065")  # province 99 does not exist
    assert not cedula_shape_ok("1770034060")  # third digit 7: not a natural person
