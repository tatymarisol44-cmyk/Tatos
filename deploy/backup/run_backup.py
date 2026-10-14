"""The daily backup from the owner's computer (docs/runbooks/backup.md, SRS RF-PRV-04).

    .venv\\Scripts\\python.exe deploy\\backup\\run_backup.py C:\\Users\\<you>\\agency-render.env

Reads the private file (SUPABASE_POOLER_URL and SUPABASE_PASSWORD, as for the Render
setup, plus BACKUP_PUBLIC_KEY and, optionally, BACKUP_DIR), writes one encrypted backup
named by its date and time, with PSEUDONYM_KEY inside it when the file has it, keeps the
newest KEEP and prints the summary (counts and secret names, never data or secrets).
register-task.ps1 runs it every day the computer is on; a day the computer was off is
caught up when it starts.

The first time, `--new-key` makes the key pair: the recovery code goes to a sheet on the
Desktop for the owner to copy by hand and delete, the public key into the private file.
It then makes the first backup and opens it with that code, so the pair is proven to work.
"""

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
SHEET = Path.home() / "Desktop" / "CODIGO-RESPALDO.txt"
# Carried inside the encryption, so the recovery code alone restores everything.
CARRIED = ("PSEUDONYM_KEY",)


def backup_name(now: datetime) -> str:
    return f"agency-backup-{now.astimezone(UTC):%Y%m%d-%H%M%S}.agbk"


def prune(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the newest `keep` backups (by name: the name is the date)."""
    backups = sorted(directory.glob(PATTERN))
    old = backups[:-keep] if len(backups) > keep else []
    for path in old:
        path.unlink()
    return old


def new_key(secrets: Path, values: dict[str, str], sheet: Path) -> str:
    """Make the pair: the code to the sheet, the public key to the private file."""
    from orchestrator import backup

    if values.get("BACKUP_PUBLIC_KEY"):
        raise setup.SetupError(
            "BACKUP_PUBLIC_KEY is already set: a new key would not open the older backups"
        )
    if sheet.exists():
        raise setup.SetupError(f"{sheet} exists: copy its code and delete it first")
    public, code = backup.recovery_code()
    sheet.write_text(backup.recovery_sheet(code), encoding="utf-8")
    setup.save_value(secrets, "BACKUP_PUBLIC_KEY", public)
    values["BACKUP_PUBLIC_KEY"] = public
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("secrets", type=Path, help="the private file with the values")
    parser.add_argument("--new-key", action="store_true", help="first time: make the key pair")
    parser.add_argument("--sheet", type=Path, default=SHEET, help="where the code is written")
    args = parser.parse_args(argv)
    try:
        values = setup.read_values(args.secrets)
        if args.new_key:
            new_key(args.secrets, values, args.sheet)
            print(f"Recovery code written to {args.sheet}; public key saved in {args.secrets}.")
    except setup.SetupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 2
    public = values.get("BACKUP_PUBLIC_KEY")
    if not public:
        print("backup: run this once with --new-key to make the key pair", file=sys.stderr)
        return 2
    directory = Path(values.get("BACKUP_DIR") or Path.home() / "agency-backups")
    directory.mkdir(parents=True, exist_ok=True)
    database, _ = setup.database_urls(
        values["SUPABASE_POOLER_URL"], values["SUPABASE_PASSWORD"], str(setup.LOCAL_SUPABASE_CA)
    )
    os.environ["DATABASE_URL"] = database
    carried = [name for name in CARRIED if values.get(name)]
    for name in carried:
        os.environ[name] = values[name]

    from orchestrator import cli

    out = directory / backup_name(datetime.now(UTC))
    create = ["backup", "create", "--out", str(out), "--recipient", public]
    for name in carried:
        create += ["--with-secret", name]
    code = cli.main(create)
    if code != 0:
        return code
    for path in prune(directory):
        print(f"removed the old backup {path.name}")
    if args.new_key:  # prove the pair: the first backup opens with the code just written
        return cli.main(
            ["backup", "restore", "--in", str(out), "--key-file", str(args.sheet), "--check"]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
