# The practice's own encrypted backup (free profile, ADR 0022)

Supabase's free plan keeps daily backups the practice cannot download, and a paused or
deleted project takes them with it. So the practice keeps its own copy, away from the
provider (SRS RF-PRV-04). It is made by `agency backup` (`src/orchestrator/backup.py`)
and needs no tools beyond the repository's Python environment. `pg_dump` and the cloud
buckets of [restore.md](restore.md) belong to the Google Cloud profile.

| | Free profile |
|---|---|
| RPO | 24 hours, on days the owner's computer is on (a missed day runs when it starts) |
| RTO | about 1 hour: a new Supabase project, `agency db upgrade`, restore, Render pointed at it |
| Where | the owner's computer (`BACKUP_DIR`, default `%USERPROFILE%\agency-backups`), newest 30 kept |
| Verified | the restore drill runs in CI on every commit (`tests/test_backup.py`); rehearse it by hand every quarter |

## How it is protected

- **Encrypted for one person.** The backup is locked with a public key, and only the
  matching private key opens it: X25519, HKDF-SHA256 and AES-256-GCM in 1 MiB chunks.
  Each chunk's nonce carries its number and a last-chunk mark (STREAM), so a file that
  was altered, reordered or cut short does not open.
- **The private half is not on the computer that makes the backups.** It is a recovery
  code written by hand on paper, in two places (or a key file on a USB drive). The code
  has 28 characters in 7 groups of 4, no O, I, L or U to misread, and a 2-character
  checksum that catches a copying mistake. Its 128 random bits are far beyond guessing.
  Losing it makes every backup useless. If it leaks, make a new pair and delete the old
  backups.
- **Never in the public repository.** GitHub artifacts of a public repository can be
  downloaded by anyone signed in, so even encrypted copies of health data do not go there.
- **One code restores everything.** The daily task carries `PSEUDONYM_KEY` (it unseals
  the inbox and matches opt-outs) inside the encrypted backup. The provider's own backups
  never hold it.

## Set up (once)

1. Make the pair and the first backup, and prove they work:
   `.venv\Scripts\python.exe deploy\backup\run_backup.py %USERPROFILE%\agency-render.env --new-key`
   It writes the recovery code to `Desktop\CODIGO-RESPALDO.txt` and the public key into the
   private file (`BACKUP_PUBLIC_KEY`). It then makes a backup and opens it with that code.
2. The owner copies the code by hand twice, checks each group, deletes the sheet and empties
   the recycle bin.
3. Prove the paper copy, typing it at the prompt (nothing is shown):
   `.venv\Scripts\python.exe -m orchestrator.cli backup restore --in <newest backup> --check`
4. Schedule it: `powershell -ExecutionPolicy Bypass -File deploy\backup\register-task.ps1`.

## Restore

1. **Into an empty database, never over the live one.** Create a new Supabase project, or
   a fresh database, and migrate it to the backup's revision:
   `DATABASE_URL=<new> agency db upgrade`. The restore refuses a different revision or
   tables that already hold rows.
2. `DATABASE_URL=<new> agency backup restore --in <file> --secrets-out <private file>.restored`
   and type the recovery code. It loads every table in one transaction: on any error
   nothing is kept. The secrets carried in the backup (`PSEUDONYM_KEY`) are written to the
   file named.
3. Point Render at the new database (the private file's Supabase values, with
   `PSEUDONYM_KEY` from that file, then `deploy/render/setup.py`).
4. Rebuild the vector index: `agency knowledge reindex` (as in [restore.md](restore.md)).
5. Check: the console signs in, a patient's record opens, `agency audit-verify` passes.

## Rehearsal log

| Date | Backup used | Duration | Who | Notes |
|---|---|---|---|---|
| | | | | |
