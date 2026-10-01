# ADR 0013: Campaign measurement — one eligible population, intention to treat, one final analysis

**Status:** accepted (supersedes the measurement parts of ADR 0012)

## Context

The external audit (findings A16–A20) showed that the campaign figures could not be read as causal effects:

- **A16.** Eligibility filters (consent, channel) applied only to the treatment arm. The analysis compared "messages that went out" against "everyone held out", so the arms came from different populations. The bias is not always conservative, as ADR 0012 claimed.
- **A17.** Dry runs (no bot token) counted as treatment, used up the patient's monthly cap and sat next to real deliveries in the history.
- **A18.** A p-value from a z-test was reported as soon as the campaign was sent, and again on every request. The conversion window had not closed, and the normal approximation is poor for rare conversions.
- **A19.** The `analytics` consent had no technical effect: patients who denied it were still segmented and ranked.
- **A20.** Restricted patients (erasure requested) still fed the forecast and the upcoming-appointment counts, so the totals did not agree.

## Decision

**Who is in the experiment (A16).** Eligibility is decided once, when the campaign is created, before arms are assigned. A patient is eligible when all of these hold:

- they are not restricted;
- they have the `analytics` consent (needed for the segment);
- they have the `marketing` consent;
- they have a working channel: an active patient-app key, or a Telegram address when Telegram is configured or only simulated.

Only eligible patients are assigned, with the existing `sha256(campaign, subject)` split. The campaign stores the segment size, the eligible count and the exclusion reasons (`population`).

**Primary analysis: intention to treat.** Each arm counts everyone assigned to it, whatever happened to their delivery:

- skips after assignment (consent withdrawn, monthly cap) stay in their arm;
- failed and uncertain deliveries also stay in their arm.

Time zero is the moment the campaign was queued, the same for both arms. A conversion is an appointment created in `(t0, t0 + CAMPAIGN_CONVERSION_WINDOW_DAYS]`. Attrition is reported per arm by status, including rows anonymised after an erasure, which can no longer be scored. Per-delivery figures (treated patients actually reached) are shown as descriptive only: who is reached is not random.

**When there is an answer (A18).**
- Before `t0 + window` the result is `provisional`: counts and rates, but no test and no conclusion.
- After that it is `final`. Bookings outside the window never count, so the final analysis is the same every time it is requested. That makes it one pre-specified look, not repeated testing.

**How it is tested (A18).**
- Fisher's exact test (two-sided), Wilson 95 % intervals per arm and Newcombe's hybrid score interval for the difference (`stats.py`). They are checked against published values and SciPy. Unlike the z-test, they are valid for zero, rare and small counts.
- The conclusion has four possible outcomes:
  - "inconclusive" when either arm has fewer than 30 patients;
  - "treatment booked more than control (significant at 5%)";
  - "treatment booked less than control (significant at 5%)";
  - "no detectable difference at 5%".
- The result carries a note: significance is not practical importance. The interval on the lift is what an owner should weigh against the campaign's cost.

**Simulation is a mode, not an accident (A17).** `mode` is fixed at creation and is either `live` (the default) or `simulation`.
- **A simulation** runs the same checks and records `dry_run` for each treated patient. It delivers nothing, not even in the app, takes no room under the cap and returns `status: simulation` with no effect.
- **A live campaign** without a bot token simply has no Telegram channel. It never records `dry_run`.

**Populations in insights (A19, A20).**
- Every metric excludes restricted patients, the forecast and upcoming appointments included.
- Operational aggregates do not depend on consent: counts, no-shows, pipeline, forecast and alerts are part of running the clinic (GDPR Art. 9(2)(h); HIPAA operations).
- Profiling a person does depend on consent: the RFM segment, the high-value ranking and therefore campaign targeting require the `analytics` consent, opt-in, where no record means no.
- The summary states both population sizes.

## Consequences

- Lift now estimates the effect of *assigning* the campaign among eligible patients. That is what an owner can decide on. It is smaller than the effect of *receiving* it when many deliveries fail, and the attrition table shows that gap.
- A newly sent campaign shows no conclusion for 30 days. That is intended.
- Patients without the `analytics` consent disappear from segments and campaigns. Clinics must collect that consent (the patient app already offers it) before campaigns reach anyone.
- Still open:
  - a minimum practically important lift per pack;
  - power calculations before sending;
  - sequential designs for owners who want earlier answers.
