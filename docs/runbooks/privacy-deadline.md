# PrivacyDeadlineAtRisk

**What it means.** At least one step in the privacy-case register is due within 24 hours or
is already late. A step is one of these:

- the answer to a rights request (15 days, LOPDP Arts. 13–16);
- a breach notification:
  - to the clinic: 2 days;
  - to the SPDP and ARCOTEL: 5 days;
  - to the affected patients: 3 days.

These are legal deadlines, not service objectives. The tools count calendar days, which
is never later than the law.

**Who acts.** The clinic's privacy officer (role `privacy`) completes the step. The
platform on-call only makes sure that person knows. For a breach that the platform found
itself, the platform's own officer acts on the "notify the clinic" step.

## Steps

1. Find the case. With a `privacy` key of each clinic, call
   `GET /v1/privacy/cases?status=open` and look for steps with `hours_left` under 24 or
   `late: true`. The alert has no tenant label, on purpose: alerts carry no personal
   data. The register answers per clinic.
2. Tell that clinic's privacy officer by phone. Note the time in the incident note.
3. The officer completes the step and records what was done:
   `POST /v1/privacy/cases/{id}/steps/{step}` with an `outcome`. Never include clinical
   content. For `notify_subjects`, "not required" is valid when the breach puts no rights
   at risk; the outcome must give the reason (LOPDP Art. 46).
4. Missed already? Complete the step anyway, with the reason for the delay. For a breach,
   the law requires the late notice to explain why (Art. 43). Then follow
   [data-breach.md](data-breach.md).
5. The officer closes the case once every step is done: `POST /v1/privacy/cases/{id}/close`.
   The audit trail records any late steps.
