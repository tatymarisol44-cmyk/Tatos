# ADR 0020: Agenda, reminders, and a WhatsApp assistant that writes like a person

**Status:** accepted (2026-10-08). Requested by the owner; the design decisions below are mine, under the owner's authority. The lawyer should still review the patient-facing wording (L3).

## Context

The owner asked for three things:

- WhatsApp should answer "like an LLM", warmly, without any medical advice;
- it should be connected to the professional's and the patient's agendas;
- crisis messages must still be handled with the utmost care.

## Decision

1. **Agenda** (`agenda.py`).
   - Each professional has weekly hours in the practice's time zone.
   - Free slots are computed from those hours, minus booked visits.
   - **No double booking:** a booking locks the professional's row and refuses any overlap, so two replicas cannot both take a slot.
   - Patients book only free slots.
   - Phones get **iCalendar feeds** signed with HMAC, showing initials and times only.
2. **Warm fixed replies** (`auto_reply.py`).
   - Crisis wording, a request for a person and STOP get **fixed, reviewed texts**.
   - The crisis text carries ECU 911 and MSP line 171 option 6, verified on the Ministry's own page.
   - Crisis messages **never reach the model**.
3. **The assistant** (`whatsapp_assistant.py`).
   - Everyday messages are answered by the model. It may use only:
     - the practice's knowledge base;
     - the person's first name and next visit;
     - three real free slots.
   - Deterministic checks run **after** the model: medication, dosage, diagnosis, therapeutic advice or techniques, links, length. Any hit, or any model failure, falls back to the fixed text.
   - **Booking is done by code:** the offered slots are stored as numbered options, without the message text. A reply "1", "2" or "3" books that slot for the patient whose number matches, a unique match only. An unknown number reaches a person instead.
4. **Reminders** (`reminders.py`).
   - One reminder per visit, about a day before, with nothing clinical in it.
   - Channel: Telegram, else an approved WhatsApp template, else the patient's app.
   - A WhatsApp STOP is respected.
   - Each visit is claimed, so any number of replicas sends one reminder.
5. **Cost:** the model's monthly spend per practice is capped (`spend.py`). Over the cap the assistant falls back to the fixed texts, and nothing else stops.

## Consequences

- **A data processing agreement is needed first:** the WhatsApp message text goes to the model provider for that one answer and is never stored. Before real patients, the provider must be under a data processing agreement, with zero retention where offered (`docs/LAUNCH.md`, step 5).
- **Meta must approve two templates** before WhatsApp can send them outside the 24-hour window: one for reminders (`WHATSAPP_REMINDER_TEMPLATE`) and one for staff alerts (`WHATSAPP_ALERT_TEMPLATE`).
- **No conversation memory:** the assistant does not remember earlier turns beyond the numbered options, because storing chat text would contradict the privacy design. The person rephrases instead.
