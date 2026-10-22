"""Verify an OCI export and select exact runtime manifests for scanning."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MEMBER = re.compile(r"(?:oci-layout|index\.json|blobs/sha256/[0-9a-f]{64})")


def unpack(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        seen = set()
        for member in source:
            name = member.name.removeprefix("./").rstrip("/")
            if member.isdir() and name in {".", "blobs", "blobs/sha256"}:
                continue
            if not member.isfile() or not MEMBER.fullmatch(name) or name in seen:
                raise ValueError(f"Invalid or duplicate OCI archive member: {member.name}")
            seen.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(member)
            assert stream is not None
            with stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output)
            if name.startswith("blobs/"):
                with target.open("rb") as content:
                    actual = hashlib.file_digest(content, "sha256").hexdigest()
                if actual != target.name:
                    raise ValueError(f"OCI blob integrity mismatch: {name}")


def inspect_layout(layout: Path, expected: str, required: set[str]) -> dict[str, str]:
    if not DIGEST.fullmatch(expected):
        raise ValueError("Invalid expected OCI digest")
    if json.loads((layout / "oci-layout").read_bytes()).get("imageLayoutVersion") != "1.0.0":
        raise ValueError("Unsupported OCI layout version")
    visited: set[str] = set()
    platforms: dict[str, str] = {}

    def blob(descriptor: dict[str, Any]) -> dict[str, Any]:
        digest = descriptor["digest"]
        if not DIGEST.fullmatch(digest):
            raise ValueError("Invalid OCI descriptor digest")
        path = layout / "blobs/sha256" / digest.removeprefix("sha256:")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("OCI JSON metadata exceeds the permitted size")
        raw = path.read_bytes()
        if len(raw) != descriptor["size"] or "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("OCI descriptor integrity mismatch")
        return json.loads(raw)

    def walk(descriptor: dict[str, Any]) -> None:
        digest = descriptor["digest"]
        if digest in visited:
            raise ValueError("Duplicate or cyclic OCI manifest")
        visited.add(digest)
        data = blob(descriptor)
        if "manifests" in data:
            for child in data["manifests"]:
                walk(child)
            return
        config = blob(data["config"])
        for layer in data["layers"]:
            digest = layer["digest"]
            if not DIGEST.fullmatch(digest):
                raise ValueError("Invalid layer digest")
            path = layout / "blobs/sha256" / digest.removeprefix("sha256:")
            if path.stat().st_size != layer["size"]:
                raise ValueError("OCI layer size mismatch")
        annotations = descriptor.get("annotations", {})
        if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
            return
        platform = f"{config.get('os')}/{config.get('architecture')}"
        declared = descriptor.get("platform")
        if declared and platform != f"{declared.get('os')}/{declared.get('architecture')}":
            raise ValueError("OCI runtime platform differs from its descriptor")
        if platform in platforms:
            raise ValueError(f"Duplicate runtime platform: {platform}")
        platforms[platform] = digest

    raw_root = (layout / "index.json").read_bytes()
    root = json.loads(raw_root)
    if "sha256:" + hashlib.sha256(raw_root).hexdigest() == expected:
        visited.add(expected)
    elif len(root["manifests"]) != 1 or root["manifests"][0]["digest"] != expected:
        raise ValueError("Exported root does not select the expected build digest")
    for descriptor in root["manifests"]:
        walk(descriptor)
    if expected not in visited:
        raise ValueError("Build digest is absent from the exported OCI graph")
    if set(platforms) != required:
        raise ValueError(
            f"Unexpected runtime platforms: {sorted(platforms)}; expected {sorted(required)}"
        )
    return platforms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--platform", action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    unpack(args.archive, args.destination)
    platforms = inspect_layout(args.destination, args.digest, set(args.platform))
    with args.archive.open("rb") as archive:
        archive_sha256 = hashlib.file_digest(archive, "sha256").hexdigest()
    if args.output:
        with args.output.open("a", encoding="utf-8") as output:
            output.write(f"archive_sha256={archive_sha256}\n")
            for platform, digest in sorted(platforms.items()):
                output.write(f"{platform.split('/')[1]}={digest}\n")
    print(json.dumps({"digest": args.digest, "scanned_runtime_manifests": platforms}, indent=2))


if __name__ == "__main__":
    main()
