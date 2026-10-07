"""Creatives by code (ADR 0015, M2): the copy is vetted before anything is drawn, Spanish
renders correctly, and the files are what Instagram and TikTok accept."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from orchestrator.config import Settings
from orchestrator.creatives import (
    HEALTH_FOOTER,
    SIZES,
    Brief,
    CreativeError,
    CreativeRejected,
    CreativeUnavailable,
    _font,
    draw_slides,
    footer_lines,
    render_infographic,
    render_video,
)
from orchestrator.packs import load_packs

PSY = load_packs()["ec-psychologist"]
GENERAL = load_packs()["general"]


def brief(**overrides: object) -> Brief:
    data: dict[str, object] = {
        "title": "Cuidar tu mente también es salud",
        "points": ["Sentir ansiedad a veces es común.", "Procura dormir bien cada día."],
        "cta": "Agenda tu cita en línea",
        "practice_name": "Consultorio Demo",
        **overrides,
    }
    return Brief.model_validate(data)


def ffmpeg() -> str | None:
    return os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")


# --- the brief and the copy rules -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "   "},
        {"points": []},
        {"points": ["uno", "dos", "tres", "cuatro", "cinco", "seis"]},
        {"points": ["x" * 121]},
        {"points": ["  "]},
        {"accent": "red"},
    ],
)
def test_bad_briefs_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        brief(**bad)


@pytest.mark.parametrize(
    ("field", "text", "violation"),
    [
        ("title", "Te ofrecemos la cura definitiva", "banned_claim:la cura"),
        ("cta", "Resultados garantizados", "banned_claim:garantizado"),
        ("points", ["Te damos tu diagnóstico en una sesión"], "clinical_detail:diagnostico"),
    ],
)
def test_forbidden_copy_never_becomes_a_file(
    tmp_path: Path, field: str, text: object, violation: str
) -> None:
    out = tmp_path / "ad.jpg"
    with pytest.raises(CreativeRejected) as exc:
        render_infographic(brief(**{field: text}), PSY, out)
    assert violation in exc.value.violations
    assert not out.exists()


def test_a_harmless_word_containing_cura_is_allowed(tmp_path: Path) -> None:
    render_infographic(brief(points=["Procura descansar."]), PSY, tmp_path / "ok.jpg")


def test_discount_above_the_cap_needs_the_owner(tmp_path: Path) -> None:
    creative = render_infographic(brief(cta="20% en tu primera cita"), PSY, tmp_path / "d.jpg")
    assert creative.needs_owner_approval  # the mental-health cap is 10%


def test_health_packs_carry_the_informative_footer() -> None:
    assert footer_lines(brief(), PSY) == ["Consultorio Demo", HEALTH_FOOTER]
    assert footer_lines(brief(), GENERAL) == ["Consultorio Demo"]


# --- Spanish text -----------------------------------------------------------------------


@pytest.mark.parametrize("bold", [False, True])
def test_the_font_has_every_spanish_letter(bold: bool) -> None:
    """Regression: Pillow's built-in font drew "también" as "tambi□n"."""
    font = _font(40, bold)
    missing = bytes(font.getmask(""))  # a private-use code point: the font's .notdef box
    for char in "áéíóúÁÉÍÓÚñÑüÜ¿¡":
        assert bytes(font.getmask(char)) != missing, char


# --- files ------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["feed", "story", "square"])
def test_infographic_is_a_jpeg_of_the_right_size(tmp_path: Path, fmt: str) -> None:
    out = tmp_path / "ad.jpg"
    creative = render_infographic(brief(), PSY, out, fmt)  # type: ignore[arg-type]
    with Image.open(out) as image:
        assert image.format == "JPEG" and image.size == SIZES[fmt]  # type: ignore[index]
    assert creative.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()
    assert creative.preflight("instagram", public_url=True).allowed
    assert not creative.preflight("instagram", public_url=False).allowed  # not uploaded yet


def test_slides_are_title_points_and_call_to_action() -> None:
    assert len(draw_slides(brief(), PSY)) == 1 + 2 + 1
    assert len(draw_slides(brief(cta=""), PSY)) == 1 + 2


@pytest.mark.skipif(ffmpeg() is None, reason="ffmpeg is not installed")
def test_video_is_an_mp4_that_tiktok_accepts_privately(tmp_path: Path, settings: Settings) -> None:
    configured = settings.model_copy(update={"ffmpeg_binary": ffmpeg()})
    out = tmp_path / "ad.mp4"
    creative = render_video(brief(), PSY, out, configured, seconds_per_slide=1)
    assert out.read_bytes()[4:8] == b"ftyp"  # an MP4 container
    assert (creative.width, creative.height) == SIZES["story"]
    check = creative.preflight("tiktok")
    assert check.allowed and check.visibility == "private"  # unaudited client


def test_video_without_ffmpeg_says_so(tmp_path: Path, settings: Settings) -> None:
    missing = settings.model_copy(update={"ffmpeg_binary": "definitely-not-ffmpeg-xyz"})
    with pytest.raises(CreativeUnavailable):
        render_video(brief(), PSY, tmp_path / "ad.mp4", missing)


def test_video_reports_an_ffmpeg_failure(tmp_path: Path, settings: Settings) -> None:
    # Python rejects ffmpeg's arguments and exits non-zero: a stand-in for a broken ffmpeg.
    broken = settings.model_copy(update={"ffmpeg_binary": sys.executable})
    with pytest.raises(CreativeError, match="ffmpeg failed"):
        render_video(brief(), PSY, tmp_path / "ad.mp4", broken)


def cli(
    tmp_path: Path,
    kind: str,
    *,
    title: str = "Cuidar tu mente es salud",
    pack: str = "ec-psychologist",
) -> list[str]:
    return [
        "creative",
        kind,
        "--pack",
        pack,
        "--title",
        title,
        "--point",
        "Hablarlo ayuda.",
        "--practice",
        "Consultorio Demo",
        "--out",
        str(tmp_path / "ad.jpg"),
    ]


def test_cli_renders_and_reports_preflight(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from orchestrator.cli import main

    assert main(cli(tmp_path, "infographic")) == 0
    out = capsys.readouterr().out
    assert '"media": "image/jpeg"' in out and '"visibility": "private"' in out  # tiktok
    assert (tmp_path / "ad.jpg").exists()


def test_cli_refuses_forbidden_copy_and_bad_packs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from orchestrator.cli import main

    assert main(cli(tmp_path, "infographic", title="La cura definitiva")) == 1
    assert "copy rejected" in capsys.readouterr().err
    assert main([*cli(tmp_path, "infographic"), "--point", "x" * 121]) == 1
    assert "invalid brief" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # an abstract pack cannot serve a practice
        main(cli(tmp_path, "infographic", pack="ec-mental-health-base"))


def test_forbidden_copy_is_rejected_before_ffmpeg_is_even_looked_up(
    tmp_path: Path, settings: Settings
) -> None:
    missing = settings.model_copy(update={"ffmpeg_binary": "definitely-not-ffmpeg-xyz"})
    with pytest.raises(CreativeRejected):
        render_video(brief(title="La cura definitiva"), PSY, tmp_path / "ad.mp4", missing)
