"""
Google Calendar API integration for class scheduling.
Handles authentication, event CRUD, and availability checking.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Calendar API scope (full read/write access)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Project paths
PROJECT_ROOT = Path(__file__).parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

# Config from .env
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "America/New_York")


def authenticate_calendar():
    """
    Authenticate with Google Calendar API using OAuth2.
    - Loads existing token.json if available
    - Auto-refreshes expired tokens
    - Falls back to full OAuth flow if needed (local only)
    Returns an authenticated Calendar API service object.
    """
    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Google Calendar token refreshed successfully.")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                creds = _run_oauth_flow()
        else:
            creds = _run_oauth_flow()

        # Save refreshed/new token
        with open(TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())

    service = build("calendar", "v3", credentials=creds)
    return service


def _run_oauth_flow():
    """Run the full OAuth2 flow. Only works locally (needs browser)."""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_FILE}. "
            "Download it from Google Cloud Console."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    logger.info("New Google Calendar authentication completed.")
    return creds


def check_availability(start_dt: datetime, end_dt: datetime) -> bool:
    """
    Check if a time slot is available (not busy) on the calendar.

    Args:
        start_dt: Start datetime (timezone-aware or naive, will use CALENDAR_TIMEZONE)
        end_dt: End datetime

    Returns:
        True if the slot is free, False if busy.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    # Ensure timezone-aware
    if start_dt.tzinfo is None:
        start_dt = tz.localize(start_dt)
    if end_dt.tzinfo is None:
        end_dt = tz.localize(end_dt)

    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items": [{"id": CALENDAR_ID}],
    }

    try:
        result = service.freebusy().query(body=body).execute()
        busy_periods = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
        return len(busy_periods) == 0
    except HttpError as e:
        logger.error(f"Error checking availability: {e}")
        raise


def list_available_slots(
    date: datetime, duration_minutes: int = 60, start_hour: int = 9, end_hour: int = 17
) -> list:
    """
    Find all available time slots on a given date.

    Args:
        date: The date to check (only date part is used)
        duration_minutes: Length of each slot in minutes
        start_hour: Working day start (24h format)
        end_hour: Working day end (24h format)

    Returns:
        List of dicts with 'start' and 'end' datetime objects for free slots.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    # Build the day range
    day_start = tz.localize(datetime(date.year, date.month, date.day, start_hour, 0))
    day_end = tz.localize(datetime(date.year, date.month, date.day, end_hour, 0))

    # Get all busy periods for the day
    body = {
        "timeMin": day_start.isoformat(),
        "timeMax": day_end.isoformat(),
        "items": [{"id": CALENDAR_ID}],
    }

    try:
        result = service.freebusy().query(body=body).execute()
        busy_periods = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    except HttpError as e:
        logger.error(f"Error listing available slots: {e}")
        raise

    # Parse busy periods into datetime objects
    busy = []
    for period in busy_periods:
        busy_start = datetime.fromisoformat(period["start"]).astimezone(tz)
        busy_end = datetime.fromisoformat(period["end"]).astimezone(tz)
        busy.append((busy_start, busy_end))

    # Find free slots by scanning in duration_minutes increments
    available = []
    current = day_start
    slot_delta = timedelta(minutes=duration_minutes)

    while current + slot_delta <= day_end:
        slot_end = current + slot_delta
        is_free = True

        for busy_start, busy_end in busy:
            # Check if slot overlaps with any busy period
            if current < busy_end and slot_end > busy_start:
                is_free = False
                break

        if is_free:
            available.append({"start": current, "end": slot_end})

        current += timedelta(minutes=30)  # Slide by 30min for more options

    return available


def create_event(
    summary: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str = None,
    attendee_email: str = None,
) -> str:
    """
    Create a new Google Calendar event.

    Args:
        summary: Event title (e.g., "Class with John")
        start_dt: Event start datetime
        end_dt: Event end datetime
        description: Optional event description
        attendee_email: Optional attendee email

    Returns:
        The created event's ID.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    if start_dt.tzinfo is None:
        start_dt = tz.localize(start_dt)
    if end_dt.tzinfo is None:
        end_dt = tz.localize(end_dt)

    event_body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 30}],
        },
    }

    if description:
        event_body["description"] = description

    if attendee_email:
        event_body["attendees"] = [{"email": attendee_email}]

    try:
        event = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        event_id = event.get("id")
        logger.info(f"Event created: {event_id} - {summary}")
        return event_id
    except HttpError as e:
        logger.error(f"Error creating event: {e}")
        raise


def update_event(event_id: str, **kwargs) -> bool:
    """
    Update an existing Google Calendar event.

    Args:
        event_id: The Google Calendar event ID
        **kwargs: Fields to update. Supported:
            - summary (str)
            - start_dt (datetime)
            - end_dt (datetime)
            - description (str)

    Returns:
        True if update succeeded.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    try:
        # Fetch current event
        event = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

        # Apply updates
        if "summary" in kwargs:
            event["summary"] = kwargs["summary"]

        if "description" in kwargs:
            event["description"] = kwargs["description"]

        if "start_dt" in kwargs:
            start = kwargs["start_dt"]
            if start.tzinfo is None:
                start = tz.localize(start)
            event["start"] = {"dateTime": start.isoformat(), "timeZone": CALENDAR_TIMEZONE}

        if "end_dt" in kwargs:
            end = kwargs["end_dt"]
            if end.tzinfo is None:
                end = tz.localize(end)
            event["end"] = {"dateTime": end.isoformat(), "timeZone": CALENDAR_TIMEZONE}

        service.events().update(
            calendarId=CALENDAR_ID, eventId=event_id, body=event
        ).execute()

        logger.info(f"Event updated: {event_id}")
        return True
    except HttpError as e:
        logger.error(f"Error updating event {event_id}: {e}")
        raise


def delete_event(event_id: str) -> bool:
    """
    Delete a Google Calendar event.

    Args:
        event_id: The Google Calendar event ID

    Returns:
        True if deletion succeeded.
    """
    service = authenticate_calendar()

    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        logger.info(f"Event deleted: {event_id}")
        return True
    except HttpError as e:
        logger.error(f"Error deleting event {event_id}: {e}")
        raise


def get_upcoming_events(days_ahead: int = 7, max_results: int = 20) -> list:
    """
    List upcoming events from the calendar.

    Args:
        days_ahead: How many days ahead to look
        max_results: Maximum number of events to return

    Returns:
        List of event dicts with id, summary, start, end.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    now = datetime.now(tz)
    time_max = now + timedelta(days=days_ahead)

    try:
        result = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = []
        for item in result.get("items", []):
            start_raw = item["start"].get("dateTime", item["start"].get("date"))
            end_raw = item["end"].get("dateTime", item["end"].get("date"))

            events.append(
                {
                    "id": item["id"],
                    "summary": item.get("summary", "No title"),
                    "start": start_raw,
                    "end": end_raw,
                    "description": item.get("description", ""),
                }
            )

        return events
    except HttpError as e:
        logger.error(f"Error listing events: {e}")
        raise


def find_event_by_date(date: datetime, phone_number: str = None) -> list:
    """
    Find events on a specific date. Optionally filter by phone number in description.

    Args:
        date: The date to search
        phone_number: Optional phone number to filter by (stored in event description)

    Returns:
        List of matching event dicts.
    """
    service = authenticate_calendar()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    day_start = tz.localize(datetime(date.year, date.month, date.day, 0, 0))
    day_end = tz.localize(datetime(date.year, date.month, date.day, 23, 59, 59))

    try:
        result = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        events = []
        for item in result.get("items", []):
            # If phone_number filter is set, check description
            if phone_number:
                desc = item.get("description", "")
                if phone_number not in desc:
                    continue

            start_raw = item["start"].get("dateTime", item["start"].get("date"))
            end_raw = item["end"].get("dateTime", item["end"].get("date"))

            events.append(
                {
                    "id": item["id"],
                    "summary": item.get("summary", "No title"),
                    "start": start_raw,
                    "end": end_raw,
                    "description": item.get("description", ""),
                }
            )

        return events
    except HttpError as e:
        logger.error(f"Error finding events: {e}")
        raise
