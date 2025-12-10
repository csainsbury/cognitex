"""Google Calendar API service for fetching and syncing events."""

from datetime import datetime, timedelta
from typing import Any

import structlog
from googleapiclient.discovery import build

from cognitex.services.google_auth import get_google_credentials

logger = structlog.get_logger()


class CalendarService:
    """Service for interacting with Google Calendar API."""

    def __init__(self):
        self._service = None

    @property
    def service(self):
        """Lazy-load the Calendar service."""
        if self._service is None:
            credentials = get_google_credentials()
            self._service = build("calendar", "v3", credentials=credentials)
        return self._service

    def list_calendars(self) -> list[dict]:
        """List all calendars the user has access to."""
        result = self.service.calendarList().list().execute()
        return result.get("items", [])

    def get_primary_calendar_id(self) -> str:
        """Get the primary calendar ID."""
        return "primary"

    def list_events(
        self,
        calendar_id: str = "primary",
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        max_results: int = 250,
        page_token: str | None = None,
        single_events: bool = True,
    ) -> dict:
        """
        List events from a calendar.

        Args:
            calendar_id: Calendar ID (default: primary)
            time_min: Start of time range (default: now)
            time_max: End of time range
            max_results: Maximum events per page
            page_token: Token for pagination
            single_events: Expand recurring events into instances

        Returns:
            Dict with 'items' list and 'nextPageToken' if more results
        """
        if time_min is None:
            time_min = datetime.utcnow()

        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min.isoformat() + "Z",
            "maxResults": max_results,
            "singleEvents": single_events,
            "orderBy": "startTime" if single_events else "updated",
        }

        if time_max:
            kwargs["timeMax"] = time_max.isoformat() + "Z"
        if page_token:
            kwargs["pageToken"] = page_token

        return self.service.events().list(**kwargs).execute()

    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        """Get a specific event by ID."""
        return self.service.events().get(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()


def extract_event_metadata(event: dict) -> dict:
    """
    Extract useful metadata from a Calendar event resource.

    Args:
        event: Google Calendar event resource

    Returns:
        Dict with extracted metadata
    """
    # Parse start/end times
    start = event.get("start", {})
    end = event.get("end", {})

    # Handle all-day events (date) vs timed events (dateTime)
    if "dateTime" in start:
        start_time = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        is_all_day = False
    else:
        start_time = datetime.fromisoformat(start.get("date", ""))
        is_all_day = True

    if "dateTime" in end:
        end_time = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00"))
    else:
        end_time = datetime.fromisoformat(end.get("date", ""))

    # Calculate duration in minutes
    duration_minutes = int((end_time - start_time).total_seconds() / 60)

    # Parse attendees
    attendees = []
    for attendee in event.get("attendees", []):
        attendees.append({
            "email": attendee.get("email", ""),
            "name": attendee.get("displayName", ""),
            "response_status": attendee.get("responseStatus", "needsAction"),
            "is_organizer": attendee.get("organizer", False),
            "is_self": attendee.get("self", False),
        })

    # Determine event type based on heuristics
    event_type = infer_event_type(event, attendees, duration_minutes)

    # Estimate energy impact
    energy_impact = estimate_energy_impact(event_type, duration_minutes, len(attendees))

    return {
        "gcal_id": event["id"],
        "title": event.get("summary", "(No title)"),
        "description": event.get("description", ""),
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
        "duration_minutes": duration_minutes,
        "is_all_day": is_all_day,
        "location": event.get("location", ""),
        "status": event.get("status", "confirmed"),
        "organizer_email": event.get("organizer", {}).get("email", ""),
        "attendees": attendees,
        "attendee_count": len(attendees),
        "event_type": event_type,
        "energy_impact": energy_impact,
        "is_recurring": "recurringEventId" in event,
        "hangout_link": event.get("hangoutLink", ""),
        "conference_data": bool(event.get("conferenceData")),
    }


def infer_event_type(event: dict, attendees: list[dict], duration_minutes: int) -> str:
    """
    Infer the type of event based on its properties.

    Returns one of: focus, meeting, one_on_one, interview, external, personal, admin
    """
    title = event.get("summary", "").lower()
    description = event.get("description", "").lower()
    attendee_count = len(attendees)

    # Check for common patterns
    if any(word in title for word in ["focus", "deep work", "no meetings", "blocked", "heads down"]):
        return "focus"

    if any(word in title for word in ["1:1", "1-1", "one on one", "catch up", "sync"]) and attendee_count == 2:
        return "one_on_one"

    if any(word in title for word in ["interview", "screening", "candidate"]):
        return "interview"

    if any(word in title for word in ["lunch", "coffee", "dinner", "drinks"]):
        return "personal"

    if any(word in title for word in ["standup", "stand-up", "scrum", "daily"]):
        return "standup"

    if any(word in title for word in ["review", "planning", "retro", "sprint"]):
        return "admin"

    # External meetings (attendees from different domains)
    if attendees:
        domains = set()
        for att in attendees:
            email = att.get("email", "")
            if "@" in email:
                domains.add(email.split("@")[1])
        if len(domains) > 1:
            return "external"

    # Default based on attendee count
    if attendee_count == 0:
        return "focus"
    elif attendee_count <= 2:
        return "one_on_one"
    else:
        return "meeting"


def estimate_energy_impact(event_type: str, duration_minutes: int, attendee_count: int) -> int:
    """
    Estimate the energy cost of an event (1-10 scale).

    Based on event type, duration, and number of attendees.
    """
    # Base energy by type
    base_energy = {
        "focus": 1,          # Focus time is restorative
        "standup": 1,        # Quick, routine
        "one_on_one": 2,     # Low energy
        "personal": 2,       # Usually enjoyable
        "admin": 3,          # Necessary but draining
        "meeting": 4,        # Standard meetings
        "external": 5,       # External requires more energy
        "interview": 6,      # High concentration needed
    }.get(event_type, 3)

    # Duration modifier
    if duration_minutes > 90:
        base_energy += 2
    elif duration_minutes > 60:
        base_energy += 1

    # Attendee modifier
    if attendee_count > 10:
        base_energy += 2
    elif attendee_count > 5:
        base_energy += 1

    return min(10, max(1, base_energy))


async def fetch_upcoming_events(
    calendar: CalendarService,
    days_ahead: int = 7,
) -> list[dict]:
    """
    Fetch events for the upcoming N days.

    Args:
        calendar: CalendarService instance
        days_ahead: Number of days to look ahead

    Returns:
        List of event metadata dicts
    """
    time_min = datetime.utcnow()
    time_max = time_min + timedelta(days=days_ahead)

    all_events = []
    page_token = None

    while True:
        result = calendar.list_events(
            time_min=time_min,
            time_max=time_max,
            max_results=250,
            page_token=page_token,
        )

        events = result.get("items", [])
        for event in events:
            all_events.append(extract_event_metadata(event))

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    logger.info("Fetched upcoming events", count=len(all_events), days_ahead=days_ahead)
    return all_events


async def fetch_historical_events(
    calendar: CalendarService,
    months_back: int = 1,
) -> list[dict]:
    """
    Fetch historical events from the past N months.

    Args:
        calendar: CalendarService instance
        months_back: Number of months to look back

    Returns:
        List of event metadata dicts
    """
    time_max = datetime.utcnow()
    time_min = time_max - timedelta(days=months_back * 30)

    all_events = []
    page_token = None

    while True:
        result = calendar.list_events(
            time_min=time_min,
            time_max=time_max,
            max_results=250,
            page_token=page_token,
        )

        events = result.get("items", [])
        for event in events:
            all_events.append(extract_event_metadata(event))

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    logger.info("Fetched historical events", count=len(all_events), months_back=months_back)
    return all_events
