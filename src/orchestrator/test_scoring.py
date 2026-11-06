"""Pin a configured instrument; pixels propose answers, deterministic rules score them."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator

from orchestrator.clinical_files import FileRejected
from orchestrator.instruments import PLACEHOLDER, InstrumentError, InstrumentSpec, score


class PixelAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    item_id: str = Field(pattern=r"^[\w-]{1,32}$")
    value: StrictFloat | None = None
    quality: Literal["clear", "uncertain"] = "uncertain"
    evidence: str = Field(default="", max_length=400)
    # Coordinates in the oriented image, as fractions of its width and height.
    region: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def normalized_region(self) -> PixelAnswer:
        if self.region is not None:
            left, top, right, bottom = self.region
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise ValueError("region must be normalized left,top,right,bottom")
        return self


def snapshot(definition: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind != "questionnaire":
        raise FileRejected(
            "Los dibujos tienen lectura descriptiva; no se puntúan como cuestionarios."
        )
    spec = InstrumentSpec.model_validate(definition["spec"])
    if definition["status"] != "active" or spec.scoring.method == "none" or not spec.scoring.bands:
        raise FileRejected("Elige un test activo con reglas numéricas y bandas configuradas.")
    if any(PLACEHOLDER in item.text for item in spec.items):
        raise FileRejected(
            "Completa los textos oficiales del test antes de asociarlo a una imagen."
        )
    return {
        "instrument_id": definition["instrument_id"],
        "version": definition["version"],
        "name": definition["name"],
        "spec": spec.model_dump(mode="json"),
        "proposals": [],
        "answers": {},
        "verified": False,
        "result": None,
    }


def proposals(data: dict[str, Any], detected: list[PixelAnswer]) -> dict[str, Any]:
    spec = InstrumentSpec.model_validate(data["spec"])
    grouped: dict[str, list[PixelAnswer]] = {}
    for entry in detected:
        grouped.setdefault(entry.item_id, []).append(entry)
    rows = []
    for item in spec.items:
        candidates = grouped.get(item.id, [])
        candidate = candidates[0] if len(candidates) == 1 else None
        value = None
        if candidate and candidate.quality == "clear" and candidate.value is not None:
            try:
                # Validate a single item using the same rules, without a score or band.
                isolated = spec.model_copy(
                    update={
                        "items": [item],
                        "scoring": spec.scoring.model_copy(
                            update={"method": "none", "bands": [], "subscales": {}, "alerts": []}
                        ),
                    }
                )
                value = score(isolated, {item.id: candidate.value})["answers"][item.id]
            except InstrumentError:
                pass
        rows.append(
            {
                "item_id": item.id,
                "value": value,
                "quality": "proposal" if value is not None else "needs_review",
                "evidence": candidate.evidence
                if candidate
                else "No hay una lectura única de este ítem.",
                "region": list(candidate.region) if candidate and candidate.region else None,
            }
        )
    return {**data, "proposals": rows, "answers": {}, "verified": False, "result": None}


def verified_score(data: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    result = score(InstrumentSpec.model_validate(data["spec"]), answers)
    return {**data, "answers": result["answers"], "verified": True, "result": result}


def summary(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    lines = [
        "PUNTUACIÓN CON REGLAS CONFIGURADAS",
        f"Test: {data['name']} · versión de configuración {data['version']}",
    ]
    if not data.get("verified") or not data.get("result"):
        return "\n".join([*lines, "Respuestas pendientes de comprobar; sin resultado confirmado."])
    result = data["result"]
    lines.extend(
        [
            f"Puntuación: {result['total']}",
            f"Banda: {result['band'] or 'Sin banda para este valor'}",
            "Respuestas comprobadas por la profesional:",
        ]
    )
    lines.extend(f"{item}: {value}" for item, value in data["answers"].items())
    lines.extend("Alerta: " + alert["message"] for alert in result["alerts"])
    lines.append("La banda corresponde a la configuración elegida; no establece un diagnóstico.")
    return "\n".join(lines)
