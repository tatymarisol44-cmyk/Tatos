from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from scripts.ci import oci_artifact, promote_oci


def oci_archive(path: Path, platforms: tuple[str, ...] = ("amd64", "arm64")) -> str:
    files: dict[str, bytes] = {}

    def add(value: Any, media: str) -> dict[str, Any]:
        raw = json.dumps(value, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        files["blobs/sha256/" + digest[7:]] = raw
        return {"mediaType": media, "size": len(raw), "digest": digest}

    manifests = []
    for arch in platforms:
        config = add(
            {"os": "linux", "architecture": arch}, "application/vnd.oci.image.config.v1+json"
        )
        manifest = add(
            {"schemaVersion": 2, "config": config, "layers": []},
            "application/vnd.oci.image.manifest.v1+json",
        )
        manifest["platform"] = {"os": "linux", "architecture": arch}
        manifests.append(manifest)
    att_config = add(
        {"os": "unknown", "architecture": "unknown"}, "application/vnd.oci.image.config.v1+json"
    )
    att = add(
        {"schemaVersion": 2, "config": att_config, "layers": []},
        "application/vnd.oci.image.manifest.v1+json",
    )
    att["annotations"] = {"vnd.docker.reference.type": "attestation-manifest"}
    manifests.append(att)
    image = add(
        {"schemaVersion": 2, "manifests": manifests}, "application/vnd.oci.image.index.v1+json"
    )
    files["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [image]}).encode()
    files["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    with tarfile.open(path, "w") as archive:
        for name, raw in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
    return str(image["digest"])


def test_export_preserves_both_platforms_and_attestations(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    digest = oci_archive(archive)
    layout = tmp_path / "layout"
    oci_artifact.unpack(archive, layout)
    result = oci_artifact.inspect_layout(layout, digest, {"linux/amd64", "linux/arm64"})
    assert set(result) == {"linux/amd64", "linux/arm64"}
    assert result["linux/amd64"] != result["linux/arm64"]
    assert len(list((layout / "blobs/sha256").iterdir())) == 7


def test_missing_runtime_platform_refuses_the_candidate(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    digest = oci_archive(archive, ("amd64",))
    layout = tmp_path / "layout"
    oci_artifact.unpack(archive, layout)
    with pytest.raises(ValueError, match="platforms"):
        oci_artifact.inspect_layout(layout, digest, {"linux/amd64", "linux/arm64"})


def test_different_build_digest_is_not_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    oci_archive(archive)
    layout = tmp_path / "layout"
    oci_artifact.unpack(archive, layout)
    with pytest.raises(ValueError, match="expected build digest"):
        oci_artifact.inspect_layout(layout, "sha256:" + "0" * 64, {"linux/amd64", "linux/arm64"})


def test_corrupted_blob_fails_before_scanning(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    with tarfile.open(archive, "w") as stream:
        entry = tarfile.TarInfo("blobs/sha256/" + "0" * 64)
        entry.size = 7
        stream.addfile(entry, io.BytesIO(b"changed"))
    with pytest.raises(ValueError, match="integrity"):
        oci_artifact.unpack(archive, tmp_path / "layout")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "blobs/../../outside"])
def test_path_traversal_is_rejected(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "api.tar"
    with tarfile.open(archive, "w") as stream:
        stream.addfile(tarfile.TarInfo(name), io.BytesIO())
    with pytest.raises(ValueError, match="archive member"):
        oci_artifact.unpack(archive, tmp_path / "layout")


def test_archive_links_are_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    with tarfile.open(archive, "w") as stream:
        entry = tarfile.TarInfo("index.json")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "../outside"
        stream.addfile(entry)
    with pytest.raises(ValueError, match="archive member"):
        oci_artifact.unpack(archive, tmp_path / "layout")


def test_promotion_retains_all_manifests_and_verifies_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "api.tar"
    archive.write_bytes(b"synthetic archive")
    raw = b'{"synthetic":"index"}'
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    commands = []
    monkeypatch.setattr(promote_oci.subprocess, "check_output", lambda command: raw)
    monkeypatch.setattr(
        promote_oci.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    assert (
        promote_oci.promote(
            archive,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            digest,
            ["127.0.0.1:5000/api:RC1"],
            None,
            True,
        )
        == digest
    )
    assert len(commands) == 1
    assert "--all" in commands[0] and "--preserve-digests" in commands[0]


def test_promotion_detects_a_changed_registry_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "api.tar"
    archive.write_bytes(b"synthetic archive")
    raw = b'{"synthetic":"index"}'
    replies = iter([raw, b"changed registry manifest"])
    monkeypatch.setattr(promote_oci.subprocess, "check_output", lambda command: next(replies))
    monkeypatch.setattr(promote_oci.subprocess, "run", lambda command, **kwargs: None)
    with pytest.raises(ValueError, match="Published manifest"):
        promote_oci.promote(
            archive,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
            "sha256:" + hashlib.sha256(raw).hexdigest(),
            ["127.0.0.1:5000/api:candidate"],
            None,
            True,
        )


def test_archive_changed_after_scanning_cannot_be_promoted(tmp_path: Path) -> None:
    archive = tmp_path / "api.tar"
    archive.write_bytes(b"changed after scanning")
    with pytest.raises(ValueError, match="Archive changed"):
        promote_oci.promote(
            archive, "0" * 64, "sha256:" + "0" * 64, ["ghcr.io/synthetic/api:test"], None
        )


def test_insecure_transport_cannot_target_a_remote_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        promote_oci.promote(
            tmp_path / "unused",
            "0" * 64,
            "sha256:" + "0" * 64,
            ["ghcr.io/synthetic/api:test"],
            None,
            True,
        )


def test_pipeline_scans_each_exported_runtime_before_promoting() -> None:
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["image"]["steps"]
    builds = [step for step in steps if "docker/build-push-action@" in step.get("uses", "")]
    assert len(builds) == 2
    assert all(not step["with"].get("push", False) for step in builds)
    assert all(step["with"]["outputs"].startswith("type=oci,") for step in builds)
    api = next(step for step in builds if step["with"]["context"] == ".")
    assert set(api["with"]["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}
    scans = [
        (index, step["with"].get("input", ""))
        for index, step in enumerate(steps)
        if "trivy-action@" in step.get("uses", "")
    ]
    assert len(scans) == 3
    assert any("api-oci@" in value and "outputs.amd64" in value for _, value in scans)
    assert any("api-oci@" in value and "outputs.arm64" in value for _, value in scans)
    assert any("jvm-oci@" in value and "outputs.amd64" in value for _, value in scans)
    promotions = [
        index for index, step in enumerate(steps) if step.get("id") in {"push-api", "push-jvm"}
    ]
    assert min(promotions) > max(index for index, _ in scans)
