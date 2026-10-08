# Legal map: Ecuador (LOPDP)

This is what Ecuadorian data protection law requires of this product, read from the primary texts on 2026-10-08. It also lists the documents in this folder that meet each requirement, and what is left for the lawyer.

The documents in Spanish (`es/`) are **templates**: the clinic and the platform fill them in, and a lawyer reviews them before anyone signs. Nothing here is legal advice. It is engineering's reading of the law, with the article cited for each point, so the lawyer can check it fast.

## Sources read (primary text)

| Short name | Instrument | Where it was read |
|---|---|---|
| LOPDP | Ley Orgánica de Protección de Datos Personales, R.O. 5.º Supl. 459, 26-05-2021 | consejodecomunicacion.gob.ec (official PDF, 40 pp.) |
| SPDP-2025-0006 | Reglamento: obligación de incorporar cláusulas de protección de datos en contratos (30-04-2025) and its Annex I (model clauses) | official PDF |
| SPDP-2025-0028 | Reglamento del Delegado de Protección de Datos (31-07-2025) | spdp.gob.ec |
| SPDP-2026-0004 | Norma general de transferencias nacionales e internacionales (28-01-2026) | spdp.gob.ec |
| SPDP-2026-0005 | Norma general sobre tratamiento a gran escala (02-02-2026) | official PDF |

The General Regulation (RGLOPDP, D.E. 904, 2023) was **not** read in full. Points that depend only on it are marked *(RGLOPDP, to verify)*.

## Roles

- **Each clinic or professional = controller** (*responsable*). It decides why and how its patients' data is processed (LOPDP Art. 30 lets health professionals process their patients' health data).
- **The platform = processor** (*encargado*). It processes only on the clinic's instructions, under a written contract (LOPDP Art. 34). It must also notify the clinic of any breach within **2 days** (*término*), and of any rights request it receives directly.
- **The platform's own providers = sub-processors:** Google Cloud for hosting, Anthropic for the model, and Meta for WhatsApp. Each needs the clinic's prior written authorisation and the same obligations, flowed down (SPDP-2025-0006 Annex I, model clause x.4).

## Findings that shape the product

1. **Every clinic using the product is "large scale"**, and so is the platform (SPDP-2026-0005 Art. 14.1: processing health data or managing clinical records is large scale by direct qualification).
   - The formula gives the same answer: about 9 points for one small practice, against a threshold of 6 (Arts. 8–10). The computation is in `es/registro-actividades.md`.
   - Consequences, for the clinic and for the platform on the data it controls (Art. 12, Arts. 15–18, General provision 2):
     - an impact assessment **before** processing starts (LOPDP Art. 42);
     - a **data protection officer**, appointed and registered with the SPDP;
     - a record of processing activities, updated at least once a year;
     - privacy by design and by default;
     - an audit at least once every 12 months, with each report kept for 5 years;
     - the privacy policy must name the large-scale processing;
     - an annual compliance report, kept for 5 years.
2. **The data protection officer cannot be the psychologist, nor whoever runs the system.**
   - A person who carries out the processing has a conflict of interest (SPDP-2025-0028 Art. 18.1). So does an "implementer" (Art. 15.5).
   - It may be an **external service** under a service contract, or a company domiciled in Ecuador (Art. 12).
   - Registration with the SPDP is due within 15 days of the appointment (Art. 5). The officer must pass the SPDP's official training programme (Art. 11).
3. **Hosting abroad is not an international transfer when the provider is a processor.**
   - SPDP-2026-0004 Art. 23: *"el encargo de tratamiento no constituye una transferencia ni comunicación de datos personales"*.
   - Google Cloud (us-east1) and Anthropic, acting as processors under a contract, therefore fall under LOPDP Art. 34, not the transfer regime of Arts. 55–60.
   - Patients must still be **told** where their data is processed and by whom (LOPDP Art. 12, item 10).
   - Countries of the Andean Community count as adequate by law (SPDP-2026-0004 Art. 59).
4. **Anonymised health data needs the authority's prior approval** before any use (LOPDP Art. 31.3). The approval requires a technical protocol and a favourable report from the health authority.
   - So the roadmap's cross-clinic no-show model, and any statistics built from health data, are **off** until that approval exists.
   - The per-clinic follow-up (ADR 0019) uses the clinic's own identified data for the patient's care, so Art. 31.3 does not apply to it.
5. **Health care itself needs no separate consent** when a professional bound by secrecy provides it under a contract with the patient (LOPDP Art. 31.1). It must still respect confidentiality and professional secrecy.
   - Marketing and analytics **do** need consent: specific, free, informed and unambiguous (Art. 8), and revocable as easily as it was given.
   - The patient may object to direct marketing at any time (Art. 16.2).
   - This is how the product already works (ADR 0013).

## Deadlines (verified)

| What | Deadline | Article |
|---|---|---|
| Answer an access request | 15 days | LOPDP Art. 13 |
| Rectification and update | 15 days | Art. 14 |
| Erasure | 15 days | Art. 15 |
| Objection | 15 days | Art. 16 |
| Breach: processor → clinic | 2 days (*término*) | Art. 43; SPDP-2025-0006 Annex I, x.3 |
| Breach: clinic → SPDP **and ARCOTEL** | as soon as possible, at most 5 days (*término*); if later, give the reasons | Art. 43 |
| Breach: clinic → affected patients, when there is a risk to their rights | 3 days (*término*) | Art. 46 |
| Register the officer's appointment | 15 days (*término*) | SPDP-2025-0028 Art. 5 |
| Keep the documents that prove a transfer is lawful | at least 3 years | SPDP-2026-0004 Art. 4 |
| Keep audit reports and annual compliance reports | at least 5 years | SPDP-2026-0005 Arts. 17–18 |

The two kinds of day are not the same:

- *Término* is counted in working days, and *plazo* in calendar days. **TO VERIFY with the lawyer** under the Código Orgánico Administrativo.
- Our tools count **calendar days**, which is never later than the legal date. A deadline shown is therefore safe to meet.

## Documents

| Document | For | File |
|---|---|---|
| Privacy notice for patients | the clinic publishes it (LOPDP Art. 12; SPDP-2026-0005 Art. 18) | [es/aviso-de-privacidad.md](es/aviso-de-privacidad.md) |
| Processing agreement, clinic ↔ platform | both sign (LOPDP Art. 34; SPDP-2025-0006) | [es/contrato-de-encargo.md](es/contrato-de-encargo.md) |
| Record of processing activities + large-scale computation | the clinic keeps it, the platform keeps its own | [es/registro-actividades.md](es/registro-actividades.md) |
| Impact assessment | written before the first real patient (LOPDP Art. 42) | [es/evaluacion-de-impacto.md](es/evaluacion-de-impacto.md) |
| Consent questions (marketing, analytics, memory) | shown once in the app; the pack is the source of truth | `src/orchestrator/pack_data/ec-mental-health-base.yaml` (`consent_prompts`) and `packs.CONSENT_FOOTER` |
| Breach procedure | the platform and each clinic | [../runbooks/data-breach.md](../runbooks/data-breach.md) |
| Sub-processors | annex to the agreement | [subprocessors.md](subprocessors.md) |

## What is left for the lawyer (the last 1 %)

1. Review and sign-off of the five Spanish templates above.
2. *Término* versus *plazo* in the deadlines table.
3. The points of the RGLOPDP marked *to verify*.
4. The open questions in `docs/private/` that the texts above do not settle:
   - acting on risk seen in the diary (I1, I3);
   - AI transcription of sessions (I2);
   - the advertising rules for health services (H1);
   - access to psychotherapy notes in a data-subject request (D2).
5. Whether ARCOTEL accepts the breach notice through the same form as the SPDP.
