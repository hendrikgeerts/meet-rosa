"""Google Calendar operations. All times are local to the user's primary calendar tz."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.external_audit import audit_googleapi_execute

log = logging.getLogger(__name__)

DEFAULT_TZ = ZoneInfo("Europe/Amsterdam")


def _execute(req: Any, *, endpoint: str, note: str | None = None) -> Any:
    """Thin wrapper that pins service='gcal' for audit logging. Shared
    implementation in core.external_audit (SECURITY_REVIEW_2 MEDIUM-7
    + M2 follow-up review — prevent gmail/gcal helpers from drifting)."""
    return audit_googleapi_execute(
        req, service="gcal", endpoint=endpoint, note=note,
    )


class CalendarClient:
    def __init__(self, creds: Credentials, calendar_id: str = "primary") -> None:
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._calendar_id = calendar_id

    # --- reads -------------------------------------------------------------

    def list_events(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        resp = _execute(
            self._service.events().list(
                calendarId=self._calendar_id,
                timeMin=_rfc3339(time_min),
                timeMax=_rfc3339(time_max),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ),
            endpoint="events.list",
            note=f"max={max_results}",
        )
        return [_normalize_event(e) for e in resp.get("items", [])]

    def search_events(
        self,
        *,
        query: str,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 25,
    ) -> list[dict[str, Any]]:
        """Tekstzoek in title/description/location via Google Calendar
        `q` parameter. Returnt instances (recurring events expanded);
        elke item heeft `recurring_event_id` set als het een herhaalt-
        instance is — handig om de hele serie te kunnen updaten."""
        resp = _execute(
            self._service.events().list(
                calendarId=self._calendar_id,
                timeMin=_rfc3339(time_min),
                timeMax=_rfc3339(time_max),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                q=query,
            ),
            endpoint="events.list",
            note="search",
        )
        return [_normalize_event(e) for e in resp.get("items", [])]

    def list_today(self) -> list[dict[str, Any]]:
        now = datetime.now(DEFAULT_TZ)
        start = datetime.combine(now.date(), time(0, 0), tzinfo=DEFAULT_TZ)
        end = start + timedelta(days=1)
        return self.list_events(time_min=start, time_max=end)

    def find_free_slots(
        self,
        *,
        duration_minutes: int,
        earliest: datetime,
        latest: datetime,
        work_start_hour: int = 9,
        work_end_hour: int = 18,
    ) -> list[dict[str, str]]:
        """Return a list of {start, end} ISO strings that are free for at least
        `duration_minutes`, respecting working hours on weekdays."""
        events = self.list_events(time_min=earliest, time_max=latest, max_results=250)
        busy: list[tuple[datetime, datetime]] = []
        for ev in events:
            s = _parse_iso(ev["start"])
            e = _parse_iso(ev["end"])
            if s and e:
                busy.append((s, e))

        slots: list[dict[str, str]] = []
        day = earliest.astimezone(DEFAULT_TZ).date()
        end_day = latest.astimezone(DEFAULT_TZ).date()
        while day <= end_day:
            if day.weekday() < 5:  # Mon-Fri
                window_start = datetime.combine(day, time(work_start_hour, 0), tzinfo=DEFAULT_TZ)
                window_end = datetime.combine(day, time(work_end_hour, 0), tzinfo=DEFAULT_TZ)
                window_start = max(window_start, earliest.astimezone(DEFAULT_TZ))
                window_end = min(window_end, latest.astimezone(DEFAULT_TZ))
                cursor = window_start
                day_busy = sorted([(s, e) for s, e in busy if s.date() == day or e.date() == day])
                for bstart, bend in day_busy:
                    if bend <= cursor:
                        continue
                    if bstart >= window_end:
                        break
                    if bstart > cursor and (bstart - cursor) >= timedelta(minutes=duration_minutes):
                        slots.append({"start": cursor.isoformat(), "end": bstart.isoformat()})
                    cursor = max(cursor, bend)
                if window_end > cursor and (window_end - cursor) >= timedelta(minutes=duration_minutes):
                    slots.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
            day += timedelta(days=1)
        return slots

    # --- writes ------------------------------------------------------------

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        add_meet_link: bool = False,
        recurrence: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": _rfc3339(start), "timeZone": str(DEFAULT_TZ)},
            "end": {"dateTime": _rfc3339(end), "timeZone": str(DEFAULT_TZ)},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        if recurrence:
            # Google Calendar verwacht een list van RRULE/RDATE/EXDATE
            # strings (RFC 5545). Voorbeeld: ["RRULE:FREQ=WEEKLY;BYDAY=MO"].
            body["recurrence"] = list(recurrence)
        if add_meet_link:
            # createRequest met unieke ID → Google Calendar genereert een
            # nieuwe Meet-URL. conferenceDataVersion=1 op insert is verplicht
            # om dit veld te activeren.
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet-{int(start.timestamp())}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                },
            }

        request_kwargs: dict[str, Any] = {
            "calendarId": self._calendar_id,
            "body": body,
            "sendUpdates": "all" if attendees else "none",
        }
        if add_meet_link:
            request_kwargs["conferenceDataVersion"] = 1
        created = _execute(
            self._service.events().insert(**request_kwargs),
            endpoint="events.insert",
            note=f"attendees={len(attendees or [])}",
        )
        return _normalize_event(created)

    def update_event(self, event_id: str, **fields: Any) -> dict[str, Any]:
        current = _execute(
            self._service.events().get(calendarId=self._calendar_id, eventId=event_id),
            endpoint="events.get",
        )
        if "title" in fields:
            current["summary"] = fields["title"]
        if "start" in fields:
            current["start"] = {"dateTime": _rfc3339(fields["start"]), "timeZone": str(DEFAULT_TZ)}
        if "end" in fields:
            current["end"] = {"dateTime": _rfc3339(fields["end"]), "timeZone": str(DEFAULT_TZ)}
        if "description" in fields:
            current["description"] = fields["description"]
        if "location" in fields:
            current["location"] = fields["location"]
        if "recurrence" in fields:
            # None / [] / lege string → recurrence verwijderen (event wordt
            # weer een eenmalige afspraak). list → RRULE-list zetten.
            rec = fields["recurrence"]
            if not rec:
                current.pop("recurrence", None)
            else:
                current["recurrence"] = list(rec)
        updated = _execute(
            self._service.events().update(
                calendarId=self._calendar_id, eventId=event_id, body=current,
            ),
            endpoint="events.update",
        )
        return _normalize_event(updated)

    def delete_event(self, event_id: str) -> None:
        _execute(
            self._service.events().delete(
                calendarId=self._calendar_id, eventId=event_id, sendUpdates="all",
            ),
            endpoint="events.delete",
        )


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    """Parse ISO 8601 → timezone-aware datetime in DEFAULT_TZ.

    Mixed tz-aware en tz-naive datetimes kunnen we niet onderling
    vergelijken/sorteren — daarom hier altijd normaliseren naar
    Europe/Amsterdam zodat callers (bv. find_free_slots) veilig
    kunnen sorteren."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        # All-day events komen als 'YYYY-MM-DD' (date-only) of als naïeve
        # datetime — interpreteer als Europe/Amsterdam.
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt.astimezone(DEFAULT_TZ)


def _normalize_event(ev: dict[str, Any]) -> dict[str, Any]:
    start = ev.get("start", {})
    end = ev.get("end", {})
    # Pak de Meet-URL als die door Google is aangemaakt (entry_point_type='video').
    meet_url: str | None = None
    for entry in (ev.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            meet_url = entry["uri"]
            break
    # recurringEventId is alleen aanwezig op een instance van een recurring
    # event. Surface 'm zodat de caller (en Claude) weet of dit één
    # voorkomen of een serie is — en voor 'update hele serie' kan
    # caller dit id meegeven aan update_event.
    recurring_id = ev.get("recurringEventId")
    return {
        "id": ev.get("id"),
        "title": ev.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": ev.get("location", ""),
        "description": ev.get("description", ""),
        "attendees": [a.get("email") for a in ev.get("attendees", []) if a.get("email")],
        "link": ev.get("htmlLink", ""),
        "meet_url": meet_url,
        "recurring_event_id": recurring_id,
        "is_recurring": bool(recurring_id),
    }
