"""Explicit upload, private visual extraction, then clinician review."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.api.instruments import PatientId, ResultId
from orchestrator.api.security import require_clinician
from orchestrator.auth import Principal
from orchestrator.clinical_files import FileRejected
from orchestrator.instruments import InstrumentError
from orchestrator.test_images import ImageReportConflict, ImageReportRestricted, TestImages
from orchestrator.test_parameters import ApplicationData, TestParameters
from orchestrator.test_vision import ImageKind, VisionUnavailable

router = APIRouter(prefix="/v1/clinical", tags=["test-images"])
Clinician = Annotated[Principal, Depends(require_clinician)]


class AnalyzeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    confirmed: bool


class SaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    body: str | None = Field(default=None, max_length=20000)
    reviewed: bool = False
    parameters: TestParameters | None = None
    application: ApplicationData | None = None
    instrument_id: str | None = Field(default=None, pattern=r"^[\w-]{1,40}$")
    instrument_version: int | None = Field(default=None, ge=1)
    clear_scoring: bool = False


class ScoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    answers: dict[str, Any] = Field(max_length=200)
    confirmed: bool


def service(request: Request) -> TestImages:
    return request.app.state.orchestrator.test_images  # type: ignore[no-any-return]


def translated(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "No se encontró la imagen o el informe.")
    if isinstance(exc, ImageReportConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, ImageReportRestricted):
        return HTTPException(403, str(exc))
    if isinstance(exc, VisionUnavailable):
        return HTTPException(503, str(exc))
    return HTTPException(422, str(exc))


@router.get("/test-image-configuration")
async def configuration(request: Request, p: Clinician) -> dict[str, Any]:
    s = service(request)
    return {
        "enabled": s.vision.enabled,
        "max_bytes": s.settings.clinical_file_max_bytes,
        "formats": ["image/png", "image/jpeg"],
        "processing": "private",
        "requires_review": True,
    }


@router.post("/patients/{patient_id}/test-images", status_code=201)
async def upload(
    patient_id: PatientId,
    request: Request,
    p: Clinician,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=120)],
    kind: Annotated[ImageKind, Form()],
    parameters: Annotated[str, Form(max_length=64000)] = "{}",
    application: Annotated[str, Form(max_length=24000)] = "{}",
    instrument_id: Annotated[str, Form(pattern=r"^$|^[\w-]{1,40}$")] = "",
    instrument_version: Annotated[int | None, Form(ge=1)] = None,
) -> dict[str, Any]:
    if not title.strip():
        raise HTTPException(422, "Escribe el nombre del test.")
    content = await file.read(service(request).settings.clinical_file_max_bytes + 1)
    try:
        parameter_values = TestParameters.model_validate_json(parameters)
        application_values = ApplicationData.model_validate_json(application)
        return await service(request).upload(
            p,
            patient_id,
            filename=file.filename or "imagen",
            content=content,
            title=title.strip(),
            kind=kind,
            parameters=parameter_values,
            application=application_values,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
        )
    except ValidationError as exc:
        raise HTTPException(422, "Revisa los parámetros y los datos de aplicación.") from exc
    except (KeyError, FileRejected, ImageReportRestricted) as exc:
        raise translated(exc) from exc


@router.get("/patients/{patient_id}/test-images")
async def listing(patient_id: PatientId, request: Request, p: Clinician) -> list[dict[str, Any]]:
    try:
        return await service(request).for_patient(p, patient_id)
    except KeyError as exc:
        raise translated(exc) from exc


@router.get("/test-images/{report_id}")
async def get(report_id: ResultId, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).get(p, report_id)
    except KeyError as exc:
        raise translated(exc) from exc


@router.post("/test-images/{report_id}/analyze")
async def analyze(
    report_id: ResultId, body: AnalyzeIn, request: Request, p: Clinician
) -> dict[str, Any]:
    if not body.confirmed:
        raise HTTPException(422, "Confirma que quieres analizar esta imagen en el motor privado.")
    try:
        return await service(request).analyze(p, report_id, body.revision)
    except (
        KeyError,
        FileRejected,
        ImageReportConflict,
        ImageReportRestricted,
        VisionUnavailable,
    ) as exc:
        raise translated(exc) from exc


@router.patch("/test-images/{report_id}")
async def save(report_id: ResultId, body: SaveIn, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).save(
            p, report_id, **body.model_dump(mode="json", exclude_unset=True)
        )
    except (KeyError, FileRejected, ImageReportConflict, ImageReportRestricted) as exc:
        raise translated(exc) from exc


@router.delete("/test-images/{report_id}", status_code=204)
async def remove(report_id: ResultId, request: Request, p: Clinician, revision: int = 1) -> None:
    try:
        await service(request).remove(p, report_id, revision)
    except (KeyError, ImageReportConflict, ImageReportRestricted) as exc:
        raise translated(exc) from exc


@router.post("/test-images/{report_id}/score")
async def score_image(
    report_id: ResultId, body: ScoreIn, request: Request, p: Clinician
) -> dict[str, Any]:
    try:
        return await service(request).score_answers(p, report_id, **body.model_dump())
    except (
        KeyError,
        FileRejected,
        ImageReportConflict,
        ImageReportRestricted,
        InstrumentError,
    ) as exc:
        raise translated(exc) from exc
