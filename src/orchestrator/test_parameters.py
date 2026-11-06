"""Professional-supplied criteria and administration context, never invented norms."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TestParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: str = Field(default="", max_length=120)
    criteria: str = Field(default="", max_length=4000)
    scoring_rules: str = Field(default="", max_length=4000)
    source: str = Field(default="", max_length=500)
    reference_population: str = Field(default="", max_length=500)


class ApplicationData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    applied_on: date | None = None
    age_years: int | None = Field(default=None, ge=0, le=120)
    age_months: int | None = Field(default=None, ge=0, le=11)
    context: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def age_parts(self) -> ApplicationData:
        if self.age_months is not None and self.age_years is None:
            raise ValueError("Indica los años de edad, incluso si son cero.")
        return self


def ready(parameters: TestParameters, application: ApplicationData) -> bool:
    return bool(
        parameters.version and parameters.criteria and parameters.source and application.applied_on
    )


def context_text(parameters: TestParameters, application: ApplicationData) -> str:
    applied = application.applied_on.isoformat() if application.applied_on else "Sin registrar"
    age = "No registrada"
    if application.age_years is not None:
        age = f"{application.age_years} años"
        if application.age_months is not None:
            age += f", {application.age_months} meses"
    return "\n".join(
        [
            "PARÁMETROS APORTADOS POR LA PROFESIONAL",
            f"Versión o edición: {parameters.version or 'Sin registrar'}",
            f"Fuente o manual: {parameters.source or 'Sin registrar'}",
            f"Población de referencia: {parameters.reference_population or 'Sin registrar'}",
            f"Criterios e indicadores:\n{parameters.criteria or 'Sin registrar'}",
            "Baremos o reglas de corrección:\n"
            + (parameters.scoring_rules or "No se cargaron baremos."),
            "DATOS DE APLICACIÓN",
            f"Fecha de aplicación: {applied}",
            f"Edad al aplicar: {age}",
            f"Contexto y observaciones: {application.context or 'Sin registrar'}",
            "Referencia declarada por la profesional; su carga no valida el baremo "
            "ni automatiza la puntuación.",
        ]
    )
