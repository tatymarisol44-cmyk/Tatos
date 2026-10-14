"""The daily backup from the owner's computer (docs/runbooks/backup.md, SRS RF-PRV-04).

    .venv\\Scripts\\python.exe deploy\\backup\\run_backup.py C:\\Users\\<you>\\agency-render.env

Reads the private file (SUPABASE_POOLER_URL and SUPABASE_PASSWORD, as for the Render
setup, plus BACKUP_PUBLIC_KEY and, optionally, BACKUP_DIR), writes one encrypted backup
named by its date and time, keeps the newest KEEP and prints the summary (counts, never
data or secrets). register-task.ps1 runs it every day the computer is on; a day the
computer was off is caught up when it starts."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "render"))

import setup  # noqa: E402  (deploy/render/setup.py: the same file, the same URL rules)

KEEP = 30
PATTERN = "agency-backup-*.agbk"


def backup_name(now: datetime) -> str:
    return f"agency-backup-{now.astimezone(UTC):%Y%m%d-%H%M%S}.agbk"


def prune(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest `keep` backups (by name: the name is the date)."""
    backups = sorted(directory.glob(PATTERN))
    old = backups[:-keep] if len(backups) > keep else []
    for path in old:
        path.unlink()
    return old


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("secrets", type=Path, help="the private file with the values")
    args = parser.parse_args(argv)
    try:
        values = setup.read_values(args.secrets)
    except setup.SetupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 2
    public = values.get("BACKUP_PUBLIC_KEY")
    if not public:
        print("backup: add BACKUP_PUBLIC_KEY to the private file (agency backup keygen)")
        return 2
    directory = Path(values.get("BACKUP_DIR") or Path.home() / "agency-backups")
    directory.mkdir(parents=True, exist_ok=True)
    database, _ = setup.database_urls(
        values["SUPABASE_POOLER_URL"], values["SUPABASE_PASSWORD"], str(setup.LOCAL_SUPABASE_CA)
    )
    os.environ["DATABASE_URL"] = database

    from orchestrator import cli

    out = directory / backup_name(datetime.now(UTC))
    code = cli.main(["backup", "create", "--out", str(out), "--recipient", public])
    if code == 0:
        for path in prune(directory):
            print(f"removed the old backup {path.name}")
    return code


if __name__ == "__main__":
    sys.exit(main())
