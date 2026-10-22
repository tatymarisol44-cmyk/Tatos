"""Promote a scanned OCI archive without rebuilding or changing its manifests."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
from pathlib import Path

SKOPEO = (
    "quay.io/skopeo/stable@sha256:3cee4b44cb54d59f5b8822f8ca6379d6aafa4308834e5d9d958f5a4dc553a428"
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TAG = re.compile(r"[a-z0-9][a-z0-9./:_-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def promote(
    archive: Path,
    archive_sha256: str,
    digest: str,
    tags: list[str],
    credentials: Path | None,
    insecure_local: bool = False,
) -> str:
    if not DIGEST.fullmatch(digest) or not re.fullmatch(r"[0-9a-f]{64}", archive_sha256):
        raise ValueError("Invalid expected digest")
    if not tags or any(not TAG.fullmatch(tag) or ":" not in tag.rsplit("/", 1)[-1] for tag in tags):
        raise ValueError("Explicit image tags are required")
    if insecure_local and any(not tag.startswith(("127.0.0.1:", "localhost:")) for tag in tags):
        raise ValueError("Insecure registry transport is restricted to loopback")
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != archive_sha256:
            raise ValueError("Archive changed after validation and scanning")
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-v",
        f"{archive.resolve()}:/image.oci.tar:ro",
    ]
    auth = []
    if credentials is not None:
        if not credentials.is_file():
            raise ValueError("Registry credentials file is missing")
        command += ["-v", f"{credentials.resolve()}:/auth/config.json:ro"]
        auth = ["--authfile", "/auth/config.json"]
    command.append(SKOPEO)
    tls = ["--tls-verify=false"] if insecure_local else []
    raw = subprocess.check_output(  # noqa: S603 - fixed Docker command, validated references
        [*command, "inspect", "--raw", "oci-archive:/image.oci.tar"]
    )
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Archive root manifest differs from the scanned build")
    for tag in tags:
        subprocess.run(  # noqa: S603 - fixed Docker command, validated references
            [
                *command,
                "copy",
                "--all",
                "--preserve-digests",
                *auth,
                *(["--dest-tls-verify=false"] if insecure_local else []),
                "oci-archive:/image.oci.tar",
                f"docker://{tag}",
            ],
            check=True,
        )
        raw = subprocess.check_output(  # noqa: S603 - fixed Docker command, validated references
            [*command, "inspect", "--raw", *auth, *tls, f"docker://{tag}"]
        )
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Published manifest digest mismatch: {tag}")
        print(f"Verified {tag}@{digest}")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--insecure-local", action="store_true")
    parser.add_argument("--tags", required=True, help="Newline-separated explicit destination tags")
    args = parser.parse_args()
    digest = promote(
        args.archive,
        args.archive_sha256,
        args.digest,
        args.tags.splitlines(),
        args.credentials,
        args.insecure_local,
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"digest={digest}\n")


if __name__ == "__main__":
    main()
