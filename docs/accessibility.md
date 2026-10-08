# Accessibility

Target: **WCAG 2.1 level AA** for every screen used by clinic staff and, later, by patients.

## Checked automatically on every commit

`tests/test_console_e2e.py` runs in Chromium in CI:

- **axe-core 4.14.0** (vendored and checksummed in `tests/fixtures/a11y/`) with the WCAG 2.0/2.1 A and AA rules, in light and dark colour schemes and in each main state: empty, agent list, an answer, an answer held for review, and the Reviews tab. Any violation fails the build.
- **Keyboard:**
  - the tabs and the mode switch are a single tab stop each, and Arrow keys, Home and End move inside them (WAI-ARIA Authoring Practices);
  - focus stays visible (`:focus-visible`).
- **Errors:** they are announced (`role="alert"`) in plain language, keep what the person typed, and offer *Try again*. A bare HTTP status is never shown.
- **AI provenance:** every answer is labelled in words, not by colour alone: *AI-generated · not reviewed by a professional*, *AI draft · pending professional review*, *Reviewed and approved by a professional*, or *Edited and approved by a professional*. The API returns the same `provenance` field to every client, the patient app included.

The first run found real contrast failures, now fixed:

- the warning colour in light mode;
- the *Approve* button in both schemes;
- the risk badges in dark mode.

## Still needs a person (automated tools catch about a third of the issues)

To do before the pilot, and on every major redesign. Record the date and the result below.

| Check | How |
|---|---|
| Screen reader | NVDA + Chrome (Windows) and VoiceOver + Safari (macOS/iOS): ask, follow a held answer, approve a review |
| Zoom and reflow | 200 % and 400 % zoom at 1280 px; 320 px wide viewport: nothing cut off, no horizontal scrolling of text |
| Reduced motion | `prefers-reduced-motion`: no essential information in animation |
| Forms | each field's label, error message tied to the field, required fields announced |
| Plain language | Spanish copy for patients read by a non-technical person (reading level, no jargon) |
| Patient app | the PWA, when it exists, gets the same automated suite before release |

| Date | Check | Result | By |
|---|---|---|---|
| — | manual review | not yet done | — |
