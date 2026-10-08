"""The practice's agenda: each professional's working hours, the free slots they leave,
the professional's and the patient's appointments, and calendar feeds for their phones.

- **Local time.** Hours are written in the practice's time zone (`CLINIC_TIMEZONE`,
  America/Guayaquil by default); appointments are stored in UTC (A24).
- **No double booking.** A booking goes through `CrmService.create_appointment`, which
  locks the professional's row and refuses any overlap, on any number of replicas.
- **Calendar feeds.** An iCalendar URL each person can add to Google, Apple or Outlook
  calendar. Calendar apps cannot send our key, so the URL carries an HMAC signature of
  what it shows (a professional's or a patient's appointments). It shows initials and
  times only, never a name, a reason or a clinical detail: the calendar provider is not
  part of the care team.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Column, Integer, String, Table, and_, delete, insert, select

from orchestrator.config import Settings
from orchestrator.crm import ACTIVE_STATUSES, appointments, patients
from orchestrator.db import Database, aware, metadata
from orchestrator.establishment import professionals
from orchestrator.governance import AuditLog

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FeedKind = Literal["professional", "patient"]

professional_hours = Table(
    "professional_hours",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("professional_id", String(64), primary_key=True),
    Column("weekday", Integer, primary_key=True),  # 0 = Monday
    Column("start_minute", Integer, primary_key=True),  # minutes after local midnight
    Column("end_minute", Integer, nullable=False),
    Column("slot_minutes", Integer, nullable=False),
)


class AgendaError(ValueError):
    """Invalid hours, an unknown professional, or a time that is not a free slot."""


@dataclass(frozen=True)
class Block:
    """A weekly block of working time, e.g. Monday 09:00-13:00 in 50-minute sessions."""

    weekday: int
    start: time
    end: time
    slot_minutes: int

    @staticmethod
    def parse(day: str, span: str, slot_minutes: int) -> Block:
        if day not in DAYS:
            raise AgendaError(f"day must be one of {DAYS}")
        try:
            a, b = (time.fromisoformat(t.strip()) for t in span.split("-"))
        except ValueError as exc:
            raise AgendaError(f"hours {span!r}: write them as HH:MM-HH:MM") from exc
        if not a < b:
            raise AgendaError(f"hours {span!r}: the end must be after the start")
        if not 10 <= slot_minutes <= 240:
            raise AgendaError("a session lasts between 10 and 240 minutes")
        return Block(DAYS.index(day), a, b, slot_minutes)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


class Agenda:
    def __init__(self, db: Database, audit: AuditLog, settings: Settings) -> None:
        self.db = db
        self.audit = audit
        self.tz = ZoneInfo(settings.clinic_timezone)
        self.feed_key = hmac.new(
            settings.pseudonym_secret(), b"calendar-feed-v1", hashlib.sha256
        ).digest()

    async def _require_professional(self, tenant: str, professional_id: str) -> None:
        query = select(professionals.c.professional_id).where(
            and_(
                professionals.c.tenant == tenant,
                professionals.c.professional_id == professional_id,
                professionals.c.active.is_(True),
            )
        )
        async with self.db.engine.connect() as conn:
            if (await conn.execute(query)).first() is None:
                raise KeyError(professional_id)

    # --- working hours ----------------------------------------------------------------

    async def set_hours(
        self, tenant: str, professional_id: str, blocks: list[Block], actor: str
    ) -> list[dict[str, Any]]:
        await self._require_professional(tenant, professional_id)
        by_day: dict[int, list[Block]] = {}
        for b in blocks:
            by_day.setdefault(b.weekday, []).append(b)
        for day, day_blocks in by_day.items():
            ordered = sorted(day_blocks, key=lambda b: b.start)
            for x, y in itertools.pairwise(ordered):
                if y.start < x.end:
                    raise AgendaError(f"{DAYS[day]}: blocks overlap")
        key = and_(
            professional_hours.c.tenant == tenant,
            professional_hours.c.professional_id == professional_id,
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(delete(professional_hours).where(key))
            for b in blocks:
                await conn.execute(
                    insert(professional_hours).values(
                        tenant=tenant,
                        professional_id=professional_id,
                        weekday=b.weekday,
                        start_minute=_minutes(b.start),
                        end_minute=_minutes(b.end),
                        slot_minutes=b.slot_minutes,
                    )
                )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "agenda.hours_set",
                f"professional/{professional_id}",
                details={"blocks": len(blocks)},
            )
        return await self.hours(tenant, professional_id)

    async def hours(self, tenant: str, professional_id: str) -> list[dict[str, Any]]:
        await self._require_professional(tenant, professional_id)
        query = (
            select(professional_hours)
            .where(
                and_(
                    professional_hours.c.tenant == tenant,
                    professional_hours.c.professional_id == professional_id,
                )
            )
            .order_by(professional_hours.c.weekday, professional_hours.c.start_minute)
        )
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        return [
            {
                "day": DAYS[r.weekday],
                "hours": f"{r.start_minute // 60:02d}:{r.start_minute % 60:02d}-"
                f"{r.end_minute // 60:02d}:{r.end_minute % 60:02d}",
                "slot_minutes": r.slot_minutes,
            }
            for r in rows
        ]

    # --- free slots -------------------------------------------------------------------

    async def _booked(
        self, tenant: str, professional_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        query = select(appointments.c.starts_at, appointments.c.duration_min).where(
            and_(
                appointments.c.tenant == tenant,
                appointments.c.professional_id == professional_id,
                appointments.c.status.in_(ACTIVE_STATUSES),
                appointments.c.starts_at < end,
                appointments.c.starts_at > start - timedelta(hours=12),
            )
        )
        async with self.db.engine.connect() as conn:
            out = []
            for s, minutes in await conn.execute(query):
                begin = aware(s)
                assert begin is not None
                out.append((begin, begin + timedelta(minutes=minutes)))
            return out

    async def free_slots(
        self,
        tenant: str,
        professional_id: str,
        *,
        days: int = 7,
        now: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Free, future slots of the coming `days`, in order, as local and UTC times."""
        now = now or datetime.now(UTC)
        blocks = await self.hours(tenant, professional_id)
        if not blocks:
            return []
        local_today = now.astimezone(self.tz).date()
        horizon_end = datetime.combine(
            local_today + timedelta(days=days), time(0), self.tz
        ).astimezone(UTC)
        booked = await self._booked(tenant, professional_id, now, horizon_end)
        slots: list[dict[str, Any]] = []
        for offset in range(days):
            day: date = local_today + timedelta(days=offset)
            for b in blocks:
                if DAYS.index(b["day"]) != day.weekday():
                    continue
                first, last = (time.fromisoformat(t) for t in b["hours"].split("-"))
                cursor = datetime.combine(day, first, self.tz)
                close = datetime.combine(day, last, self.tz)
                step = timedelta(minutes=b["slot_minutes"])
                while cursor + step <= close:
                    begin, end = cursor.astimezone(UTC), (cursor + step).astimezone(UTC)
                    if begin > now and not any(s < end and e > begin for s, e in booked):
                        slots.append(
                            {
                                "starts_at": begin.isoformat(),
                                "local": cursor.strftime("%Y-%m-%d %H:%M"),
                                "weekday": DAYS[cursor.weekday()],
                                "minutes": b["slot_minutes"],
                            }
                        )
                        if len(slots) >= limit:
                            return slots
                    cursor += step
        return slots

    async def is_free_slot(self, tenant: str, professional_id: str, starts_at: datetime) -> int:
        """The slot's length if `starts_at` is a free slot of the professional, else raise."""
        start = starts_at.astimezone(UTC)
        days = max(1, (start.astimezone(self.tz).date() - datetime.now(self.tz).date()).days + 1)
        for slot in await self.free_slots(tenant, professional_id, days=min(days, 120)):
            if datetime.fromisoformat(slot["starts_at"]) == start:
                return int(slot["minutes"])
        raise AgendaError("that time is not a free slot of the professional")

    # --- the agenda itself --------------------------------------------------------------

    async def appointments_for(
        self,
        tenant: str,
        *,
        professional_id: str | None = None,
        patient_id: str | None = None,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        condition = [
            appointments.c.tenant == tenant,
            appointments.c.starts_at >= start,
            appointments.c.starts_at < end,
        ]
        if professional_id is not None:
            condition.append(appointments.c.professional_id == professional_id)
        if patient_id is not None:
            condition.append(appointments.c.patient_id == patient_id)
        query = (
            select(appointments, patients.c.display_name)
            .join(
                patients,
                and_(
                    patients.c.tenant == appointments.c.tenant,
                    patients.c.id == appointments.c.patient_id,
                ),
            )
            .where(and_(*condition))
            .order_by(appointments.c.starts_at)
        )
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        out = []
        for r in rows:
            begin = aware(r.starts_at)
            assert begin is not None
            out.append(
                {
                    "id": r.id,
                    "patient_id": r.patient_id,
                    "patient_name": r.display_name,
                    "professional_id": r.professional_id,
                    "starts_at": begin.isoformat(),
                    "local": begin.astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
                    "duration_min": r.duration_min,
                    "kind": r.kind,
                    "status": r.status,
                }
            )
        return out

    # --- calendar feeds (iCalendar) -----------------------------------------------------

    def _feed_sig(self, tenant: str, kind: FeedKind, owner: str) -> str:
        return hmac.new(
            self.feed_key, f"{tenant}|{kind}|{owner}".encode(), hashlib.sha256
        ).hexdigest()[:40]

    def feed_path(self, tenant: str, kind: FeedKind, owner: str) -> str:
        return f"/calendar/{kind}/{tenant}/{owner}/{self._feed_sig(tenant, kind, owner)}.ics"

    def feed_valid(self, tenant: str, kind: FeedKind, owner: str, sig: str) -> bool:
        return hmac.compare_digest(self._feed_sig(tenant, kind, owner), sig)

    async def ics(self, tenant: str, kind: FeedKind, owner: str, practice: str) -> str:
        now = datetime.now(UTC)
        rows = await self.appointments_for(
            tenant,
            professional_id=owner if kind == "professional" else None,
            patient_id=owner if kind == "patient" else None,
            start=now - timedelta(days=30),
            end=now + timedelta(days=180),
        )
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//agency-orchestrator//agenda//ES",
            "CALSCALE:GREGORIAN",
            f"X-WR-CALNAME:{_ics_text(practice)}",
        ]
        for r in rows:
            if r["status"] not in ACTIVE_STATUSES:
                continue
            begin = datetime.fromisoformat(r["starts_at"])
            end = begin + timedelta(minutes=r["duration_min"])
            if kind == "professional":
                initials = "".join(w[0] for w in r["patient_name"].split()[:2]).upper()
                summary = f"Sesión · {initials}"
            else:
                summary = f"Cita en {practice}"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{r['id']}@{tenant}",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{begin.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{end.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                f"SUMMARY:{_ics_text(summary)}",
                "STATUS:CONFIRMED" if r["status"] == "confirmed" else "STATUS:TENTATIVE",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


def _ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", " ")
