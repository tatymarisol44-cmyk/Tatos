# Review backlog (SLO-4)

**What it means:** more than 20 answers have been held for human review for over an hour.

1. **Which clinics?** Call `GET /v1/reviews` for each tenant with a reviewer key.
2. **Usual causes:**
   - no staff key has the `reviewer` role;
   - a pack's risk rules hold ordinary questions. Check `review.risk.reasons` in the held answers.
3. **Do not lower the risk threshold to clear the queue.** Held answers may contain clinical advice.
