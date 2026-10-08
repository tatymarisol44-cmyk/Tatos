# ADR 0019: Tests designed by the professional, clinical files, follow-up

**Status:** accepted (2026-10-08). The owner asked for these features, and the design decisions below are mine; the owner gave me authority to make them.

## Context

A psychologist must be able to bring any test they use, attach documents to the record, and check how each patient is doing, all from the console. The data involved is the most sensitive the product holds.

## Decision

1. **Instruments** (`instruments.py`).
   - A professional can define **any** test:
     - choice, number or open-text items;
     - reverse-scored items;
     - sum or mean scoring;
     - subscales, bands and per-item alert rules.
   - **Versioned:** each result keeps the version it was scored with.
   - **Visibility:** private to its author or shared with the practice. Only the author or an admin revises or retires it.
   - **Licensing:** the professional attests the right to use the test. Only public-domain templates ship (PHQ-9 and GAD-7, scored exactly like `scales.py`).
   - **Results** are health data: clinicians only, audited without the answers, and included in the data-subject export.
2. **Clinical files** (`clinical_files.py`).
   - Stored **in the database**, so they share its isolation, encryption at rest and point-in-time backups; up to 10 MB.
   - The type is proven by the content's signature.
   - SHA-256 at upload; the checksum is returned on download.
   - Author-only files are possible; every upload and download is audited.
3. **Follow-up** (`followup.py`). The owner asked to "verify the patient's behaviour", which I decided means clinical follow-up for the treating professional:
   - attendance;
   - a no-show risk that is **a rule with its reasons**: no level above "low" without a reason;
   - test trends by severity band;
   - campaign engagement **only with the analytics consent**;
   - a worklist of patients with a flag.
   - It is computed on request: nothing is stored and no model is involved.
   - The **mood diary** stays out until the lawyer answers I1/I3.

## Consequences

- The psychologist works in the console, and the API docs are no longer needed for daily work.
- Not built:
  - patients filling in tests themselves in their app (needs the patient app);
  - item texts of copyrighted tests (the professional enters them under their own licence);
  - large files (bucket storage) once a practice exceeds a few GB.
