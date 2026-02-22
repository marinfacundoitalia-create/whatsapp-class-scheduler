"""
Core conversation orchestrator for the WhatsApp Class Scheduling Agent.
Routes parsed intents to calendar actions and Playtomic API operations,
manages confirmation flows, and formats replies.

Supports two backends:
- Google Calendar: private class scheduling
- Playtomic: court bookings and club class enrollment
"""

import os
import logging
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from execution.intent_parser import parse_user_message
from execution.conversation_state import (
    get_conversation,
    update_conversation,
    get_conversation_history,
    set_pending_operation,
    get_pending_operation,
    clear_pending_operation,
    save_scheduled_event,
    get_user_events,
    find_user_event_by_date,
    delete_scheduled_event,
    save_playtomic_booking,
    get_user_playtomic_bookings,
    find_user_playtomic_booking_by_date,
    delete_playtomic_booking,
)
from execution.google_calendar import (
    check_availability,
    list_available_slots,
    create_event,
    update_event,
    delete_event,
    get_upcoming_events,
    find_event_by_date,
)
from execution.playtomic_api import (
    get_court_availability,
    check_court_available,
    get_upcoming_classes as pt_get_upcoming_classes,
    get_upcoming_lessons as pt_get_upcoming_lessons,
    create_booking as pt_create_booking,
    enroll_in_class as pt_enroll_in_class,
    cancel_booking as pt_cancel_booking,
    get_my_bookings as pt_get_my_bookings,
    find_booking_by_date as pt_find_booking_by_date,
    PlaytomicError,
    PlaytomicBookingError,
    PlaytomicSlotTakenError,
)

load_dotenv()

logger = logging.getLogger(__name__)

# Config
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "America/New_York")
OWNER_PHONE = os.getenv("OWNER_PHONE", "")
TZ = pytz.timezone(CALENDAR_TIMEZONE)


def handle_incoming_message(
    phone_number: str, message_text: str, sender_name: str = ""
) -> str:
    """
    Main entry point. Processes an incoming WhatsApp message and returns a response.

    Flow:
    1. Save incoming message to conversation history
    2. Check for pending operations (confirmation/denial)
    3. Parse intent with Claude
    4. Route to appropriate handler
    5. Save bot response to history
    6. Return response text

    Args:
        phone_number: Sender's WhatsApp number
        message_text: The message text
        sender_name: Sender's WhatsApp display name

    Returns:
        Response text to send back via WhatsApp
    """
    # Save incoming message
    update_conversation(phone_number, "user", message_text)

    # Get conversation context
    history = get_conversation_history(phone_number)
    pending = get_pending_operation(phone_number)

    # Parse intent
    parsed = parse_user_message(message_text, conversation_history=history)
    intent = parsed["intent"]
    entities = parsed["entities"]
    confidence = parsed["confidence"]

    logger.info(
        f"[{phone_number}] Intent: {intent}, Confidence: {confidence}, "
        f"Entities: {entities}"
    )

    # ── Handle confirmation/denial of pending operations ──
    if intent == "confirm" and pending:
        response = _execute_pending_operation(phone_number, pending)
    elif intent == "deny" and pending:
        clear_pending_operation(phone_number)
        response = "No problem, I've cancelled that. What would you like to do?"
    elif pending and intent not in ("confirm", "deny"):
        # User sent something else while there's a pending operation
        response = (
            f"You have a pending action: {_describe_pending(pending)}.\n\n"
            f"Reply *yes* to confirm or *no* to cancel before making a new request."
        )

    # ── Handle clarification needed ──
    elif parsed["clarification_needed"] and confidence < 0.7:
        response = parsed.get(
            "clarification_question",
            "I'm not sure I understood. Could you rephrase? For example:\n"
            '• "Schedule a class tomorrow at 3pm"\n'
            '• "Cancel my Friday class"\n'
            '• "What\'s available next Monday?"',
        )

    # ── Route by intent ──
    elif intent == "schedule":
        service = entities.get("service")
        if service == "playtomic":
            response = _handle_playtomic_schedule(phone_number, entities, sender_name)
        else:
            # Default to calendar for "schedule a class"
            response = _handle_schedule(phone_number, entities, sender_name)

    elif intent == "book_court":
        # Always Playtomic
        response = _handle_playtomic_schedule(phone_number, entities, sender_name)

    elif intent == "enroll_class":
        # Always Playtomic
        response = _handle_playtomic_enroll(phone_number, entities)

    elif intent == "reschedule":
        service = entities.get("service")
        if service == "playtomic":
            response = _handle_playtomic_reschedule(phone_number, entities)
        else:
            response = _handle_reschedule(phone_number, entities)

    elif intent == "cancel":
        service = entities.get("service")
        if service == "playtomic":
            response = _handle_playtomic_cancel(phone_number, entities)
        elif service is None:
            # Ambiguous: check both systems
            response = _handle_cancel_ambiguous(phone_number, entities)
        else:
            response = _handle_cancel(phone_number, entities)

    elif intent == "check_availability":
        service = entities.get("service")
        if service == "playtomic":
            response = _handle_playtomic_availability(entities)
        else:
            response = _handle_check_availability(entities)

    elif intent == "check_schedule":
        service = entities.get("service")
        if service == "playtomic":
            response = _handle_playtomic_schedule_check(phone_number)
        elif service is None:
            # Show both calendars
            response = _handle_combined_schedule_check(phone_number)
        else:
            response = _handle_check_schedule(phone_number)

    elif intent == "greeting":
        response = _handle_greeting(sender_name, phone_number)
    else:
        response = (
            "I can help you with:\n\n"
            "*Private classes (Calendar):*\n"
            '  "Schedule a class tomorrow at 3pm"\n'
            '  "Cancel my Friday class"\n\n'
            "*Playtomic (courts & club):*\n"
            '  "Book a court tomorrow at 5pm"\n'
            '  "What courts are available Saturday?"\n'
            '  "Show me club classes"\n'
            '  "Cancel my court booking"\n\n'
            '*Both:*\n'
            '  "Show my schedule"'
        )

    # Save bot response to history
    update_conversation(phone_number, "assistant", response)

    return response


# ──────────────────────────────────────────────
# Intent Handlers
# ──────────────────────────────────────────────


def _handle_greeting(sender_name: str, phone_number: str) -> str:
    """Handle greeting messages."""
    name = sender_name or "there"
    is_owner = phone_number == OWNER_PHONE

    if is_owner:
        return (
            f"Hi {name}! What would you like to do?\n\n"
            "As the owner, you can:\n"
            '  "Show all bookings today"\n'
            '  "Show my schedule" (Calendar + Playtomic)\n'
            '  Or any scheduling/booking command'
        )

    return (
        f"Hi {name}! I'm the scheduling assistant.\n\n"
        "*Private classes:*\n"
        '  "Schedule a class Friday at 2pm"\n'
        '  "Cancel my class"\n\n'
        "*Padel courts (Playtomic):*\n"
        '  "Book a court Saturday at 5pm"\n'
        '  "What courts are available?"\n'
        '  "Show me club classes"\n\n'
        '  "Show my schedule" — see everything'
    )


def _handle_schedule(phone_number: str, entities: dict, sender_name: str) -> str:
    """Handle scheduling a new class."""
    date_str = entities.get("date")
    time_str = entities.get("time")
    duration = entities.get("duration_minutes", 60)

    # Need both date and time
    if not date_str:
        return "What date would you like to schedule the class? (e.g., tomorrow, next Monday, Feb 20)"

    if not time_str:
        return f"What time on {_format_date(date_str)}? (e.g., 3pm, 10:00, afternoon)"

    # Build datetime objects
    try:
        start_dt = _build_datetime(date_str, time_str)
        end_dt = start_dt + timedelta(minutes=duration)
    except ValueError as e:
        return f"I couldn't understand that date/time. Could you try again? (Error: {e})"

    # Check if it's in the past
    now = datetime.now(TZ)
    if start_dt < now:
        return "That time is in the past. Please choose a future date and time."

    # Check availability
    try:
        is_available = check_availability(start_dt, end_dt)
    except Exception as e:
        logger.error(f"Calendar availability check failed: {e}")
        return "I'm having trouble checking the calendar right now. Please try again in a moment."

    if not is_available:
        # Suggest alternatives
        return _suggest_alternatives(start_dt, duration)

    # Set pending operation for confirmation
    operation_data = {
        "date": date_str,
        "time": time_str,
        "start_dt": start_dt.isoformat(),
        "end_dt": end_dt.isoformat(),
        "duration": duration,
        "student_name": sender_name or entities.get("student_name", ""),
        "summary": f"Class - {sender_name or entities.get('student_name', phone_number)}",
    }
    set_pending_operation(phone_number, "schedule", operation_data)

    formatted_date = _format_date(date_str)
    formatted_time = _format_time(time_str)

    return (
        f"I can schedule a {duration}-minute class for you:\n\n"
        f"📅 *{formatted_date}*\n"
        f"🕐 *{formatted_time}* ({CALENDAR_TIMEZONE})\n\n"
        f"Reply *yes* to confirm or *no* to cancel."
    )


def _handle_reschedule(phone_number: str, entities: dict) -> str:
    """Handle rescheduling an existing class."""
    date_str = entities.get("date")
    new_date_str = entities.get("new_date")
    new_time_str = entities.get("new_time")

    # Find the existing event
    if not date_str:
        # Try to find user's upcoming events
        user_events = get_user_events(phone_number)
        if not user_events:
            return "I don't see any upcoming classes for you. Would you like to schedule a new one?"

        if len(user_events) == 1:
            # Only one event, use it
            event = user_events[0]
            date_str = event["start_time"][:10]
        else:
            # Multiple events, ask which one
            event_list = _format_event_list(user_events[:5])
            return f"Which class would you like to reschedule?\n\n{event_list}"

    # Find the specific event
    events = find_user_event_by_date(phone_number, date_str)
    if not events:
        return f"I don't see a class for you on {_format_date(date_str)}. Can you check the date?"

    event = events[0]  # Take first match

    # Need new date/time
    if not new_date_str and not new_time_str:
        return "When would you like to move it to? (e.g., Tuesday at 3pm, next Monday 10am)"

    # Use original date if only time changed
    if not new_date_str:
        new_date_str = date_str
    if not new_time_str:
        return f"What time on {_format_date(new_date_str)}?"

    # Build new datetime
    try:
        new_start = _build_datetime(new_date_str, new_time_str)
        duration = entities.get("duration_minutes", 60)
        new_end = new_start + timedelta(minutes=duration)
    except ValueError:
        return "I couldn't understand the new date/time. Could you try again?"

    # Check availability of new slot
    try:
        is_available = check_availability(new_start, new_end)
    except Exception:
        return "I'm having trouble checking the calendar. Please try again."

    if not is_available:
        return _suggest_alternatives(new_start, duration)

    # Set pending reschedule
    operation_data = {
        "google_event_id": event["google_event_id"],
        "original_date": date_str,
        "new_date": new_date_str,
        "new_time": new_time_str,
        "new_start_dt": new_start.isoformat(),
        "new_end_dt": new_end.isoformat(),
        "summary": event.get("summary", "Class"),
    }
    set_pending_operation(phone_number, "reschedule", operation_data)

    return (
        f"Move your class from {_format_date(date_str)} to:\n\n"
        f"📅 *{_format_date(new_date_str)}*\n"
        f"🕐 *{_format_time(new_time_str)}* ({CALENDAR_TIMEZONE})\n\n"
        f"Reply *yes* to confirm or *no* to cancel."
    )


def _handle_cancel(phone_number: str, entities: dict) -> str:
    """Handle cancelling a class."""
    date_str = entities.get("date")

    if not date_str:
        # Show user's events and ask which to cancel
        user_events = get_user_events(phone_number)
        if not user_events:
            return "You don't have any upcoming classes to cancel."

        if len(user_events) == 1:
            event = user_events[0]
            date_str = event["start_time"][:10]
        else:
            event_list = _format_event_list(user_events[:5])
            return f"Which class would you like to cancel?\n\n{event_list}"

    # Find the event
    events = find_user_event_by_date(phone_number, date_str)
    if not events:
        return f"I don't see a class for you on {_format_date(date_str)}."

    event = events[0]

    # Set pending cancellation
    operation_data = {
        "google_event_id": event["google_event_id"],
        "date": date_str,
        "summary": event.get("summary", "Class"),
        "start_time": event["start_time"],
    }
    set_pending_operation(phone_number, "cancel", operation_data)

    start_time = event["start_time"]
    if "T" in start_time:
        time_part = start_time.split("T")[1][:5]
    else:
        time_part = ""

    return (
        f"Cancel your class on {_format_date(date_str)}"
        f"{' at ' + _format_time(time_part) if time_part else ''}?\n\n"
        f"Reply *yes* to confirm or *no* to keep it."
    )


def _handle_check_availability(entities: dict) -> str:
    """Show available time slots for a date."""
    date_str = entities.get("date")

    if not date_str:
        return "Which date would you like me to check? (e.g., tomorrow, next Monday, Feb 20)"

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "I couldn't understand that date. Could you try again? (e.g., 2026-02-20)"

    try:
        slots = list_available_slots(date_obj, duration_minutes=60)
    except Exception:
        return "I'm having trouble checking the calendar. Please try again."

    if not slots:
        return f"Sorry, there are no available slots on {_format_date(date_str)}. Try another day?"

    # Format available slots (show up to 6)
    slot_lines = []
    shown = 0
    for slot in slots:
        if shown >= 6:
            break
        start = slot["start"].strftime("%I:%M %p")
        end = slot["end"].strftime("%I:%M %p")
        slot_lines.append(f"• {start} – {end}")
        shown += 1

    slots_text = "\n".join(slot_lines)
    remaining = len(slots) - shown

    response = f"Available slots on *{_format_date(date_str)}*:\n\n{slots_text}"
    if remaining > 0:
        response += f"\n\n...and {remaining} more slots available."
    response += '\n\nWould you like to book one? Just say "book [time]".'

    return response


def _handle_check_schedule(phone_number: str) -> str:
    """Show user's upcoming classes."""
    is_owner = phone_number == OWNER_PHONE

    if is_owner:
        # Owner sees all upcoming events
        try:
            events = get_upcoming_events(days_ahead=7)
        except Exception:
            return "I'm having trouble accessing the calendar. Please try again."

        if not events:
            return "No upcoming classes in the next 7 days."

        lines = []
        for evt in events[:10]:
            start = evt["start"]
            if "T" in start:
                dt = datetime.fromisoformat(start)
                lines.append(
                    f"• {dt.strftime('%a %b %d')} at {dt.strftime('%I:%M %p')} — {evt['summary']}"
                )
            else:
                lines.append(f"• {start} — {evt['summary']}")

        return f"📅 *Your schedule (next 7 days):*\n\n" + "\n".join(lines)
    else:
        # Student sees only their events
        user_events = get_user_events(phone_number)
        if not user_events:
            return "You don't have any upcoming classes scheduled. Would you like to book one?"

        return f"📅 *Your upcoming classes:*\n\n{_format_event_list(user_events[:5])}"


# ──────────────────────────────────────────────
# Pending Operation Execution
# ──────────────────────────────────────────────


def _execute_pending_operation(phone_number: str, pending: dict) -> str:
    """Execute a confirmed pending operation."""
    op_type = pending["type"]
    data = pending["data"]

    try:
        if op_type == "schedule":
            return _execute_schedule(phone_number, data)
        elif op_type == "reschedule":
            return _execute_reschedule(phone_number, data)
        elif op_type == "cancel":
            return _execute_cancel(phone_number, data)
        # ── Playtomic operations ──
        elif op_type == "playtomic_book_court":
            return _execute_playtomic_book_court(phone_number, data)
        elif op_type == "playtomic_enroll_class":
            return _execute_playtomic_enroll_class(phone_number, data)
        elif op_type == "playtomic_cancel":
            return _execute_playtomic_cancel(phone_number, data)
        else:
            clear_pending_operation(phone_number)
            return "Something went wrong. Please try again."
    except Exception as e:
        logger.error(f"Failed to execute {op_type}: {e}", exc_info=True)
        clear_pending_operation(phone_number)
        return "Something went wrong while processing your request. Please try again."


def _execute_schedule(phone_number: str, data: dict) -> str:
    """Create a calendar event and save the mapping."""
    start_dt = datetime.fromisoformat(data["start_dt"])
    end_dt = datetime.fromisoformat(data["end_dt"])
    summary = data["summary"]

    try:
        # Create Google Calendar event with phone number in description for tracking
        event_id = create_event(
            summary=summary,
            start_dt=start_dt,
            end_dt=end_dt,
            description=f"Booked via WhatsApp by {phone_number}",
        )

        # Save event-to-user mapping
        save_scheduled_event(
            phone_number=phone_number,
            google_event_id=event_id,
            summary=summary,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
        )

        clear_pending_operation(phone_number)

        formatted_date = _format_date(data["date"])
        formatted_time = _format_time(data["time"])

        return (
            f"✅ *Class booked!*\n\n"
            f"📅 {formatted_date}\n"
            f"🕐 {formatted_time} ({CALENDAR_TIMEZONE})\n"
            f"⏱ {data['duration']} minutes\n\n"
            f"I'll see you then! To reschedule or cancel, just message me."
        )
    except Exception as e:
        logger.error(f"Failed to create event: {e}")
        clear_pending_operation(phone_number)
        return "Sorry, I couldn't create the calendar event. Please try again."


def _execute_reschedule(phone_number: str, data: dict) -> str:
    """Update a calendar event to a new time."""
    try:
        new_start = datetime.fromisoformat(data["new_start_dt"])
        new_end = datetime.fromisoformat(data["new_end_dt"])

        update_event(
            event_id=data["google_event_id"],
            start_dt=new_start,
            end_dt=new_end,
        )

        # Update local DB
        save_scheduled_event(
            phone_number=phone_number,
            google_event_id=data["google_event_id"],
            summary=data["summary"],
            start_time=new_start.isoformat(),
            end_time=new_end.isoformat(),
        )

        clear_pending_operation(phone_number)

        return (
            f"✅ *Class rescheduled!*\n\n"
            f"📅 {_format_date(data['new_date'])}\n"
            f"🕐 {_format_time(data['new_time'])} ({CALENDAR_TIMEZONE})\n\n"
            f"See you at the new time!"
        )
    except Exception as e:
        logger.error(f"Failed to reschedule event: {e}")
        clear_pending_operation(phone_number)
        return "Sorry, I couldn't reschedule the event. Please try again."


def _execute_cancel(phone_number: str, data: dict) -> str:
    """Delete a calendar event."""
    try:
        delete_event(data["google_event_id"])
        delete_scheduled_event(data["google_event_id"])
        clear_pending_operation(phone_number)

        return f"✅ *Class cancelled* on {_format_date(data['date'])}.\n\nWould you like to schedule a new one?"
    except Exception as e:
        logger.error(f"Failed to cancel event: {e}")
        clear_pending_operation(phone_number)
        return "Sorry, I couldn't cancel the event. Please try again."


# ──────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────


def _build_datetime(date_str: str, time_str: str) -> datetime:
    """
    Build a timezone-aware datetime from date and time strings.

    Args:
        date_str: "YYYY-MM-DD"
        time_str: "HH:MM" (24h format)

    Returns:
        Timezone-aware datetime
    """
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return TZ.localize(dt)


def _format_date(date_str: str) -> str:
    """Format a date string nicely. '2026-02-20' → 'Friday, Feb 20'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A, %b %d")
    except ValueError:
        return date_str


def _format_time(time_str: str) -> str:
    """Format a time string nicely. '15:00' → '3:00 PM'."""
    try:
        dt = datetime.strptime(time_str, "%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return time_str


def _format_event_list(events: list) -> str:
    """Format a list of events for display."""
    lines = []
    for i, evt in enumerate(events, 1):
        start = evt.get("start_time", evt.get("start", ""))
        summary = evt.get("summary", "Class")

        if "T" in start:
            dt = datetime.fromisoformat(start)
            if dt.tzinfo is None:
                dt = TZ.localize(dt)
            lines.append(
                f"{i}. {dt.strftime('%a %b %d')} at {dt.strftime('%I:%M %p')} — {summary}"
            )
        else:
            lines.append(f"{i}. {start} — {summary}")

    return "\n".join(lines)


def _suggest_alternatives(requested_dt: datetime, duration: int) -> str:
    """Suggest alternative time slots when the requested one is taken."""
    try:
        slots = list_available_slots(requested_dt, duration_minutes=duration)
    except Exception:
        return (
            f"That time is already taken, and I'm having trouble finding alternatives. "
            f"Could you suggest a different time?"
        )

    if not slots:
        return (
            f"That time is taken and there are no other openings on "
            f"{requested_dt.strftime('%A, %b %d')}. Would you like to try a different day?"
        )

    # Show up to 3 alternatives
    alt_lines = []
    for slot in slots[:3]:
        start = slot["start"].strftime("%I:%M %p")
        alt_lines.append(f"• {start}")

    alts_text = "\n".join(alt_lines)

    return (
        f"That time is already booked. Here are available slots on "
        f"{requested_dt.strftime('%A, %b %d')}:\n\n"
        f"{alts_text}\n\n"
        f'Would you like one of these? Just say "book [time]".'
    )


def _describe_pending(pending: dict) -> str:
    """Create a human-readable description of a pending operation."""
    op_type = pending["type"]
    data = pending["data"]

    if op_type == "schedule":
        return f"booking a class on {_format_date(data['date'])} at {_format_time(data['time'])}"
    elif op_type == "reschedule":
        return f"rescheduling to {_format_date(data['new_date'])} at {_format_time(data['new_time'])}"
    elif op_type == "cancel":
        return f"cancelling your class on {_format_date(data['date'])}"
    # ── Playtomic ──
    elif op_type == "playtomic_book_court":
        return f"booking {data.get('resource_name', 'a court')} on {_format_date(data['date'])} at {_format_time(data['time'])}"
    elif op_type == "playtomic_enroll_class":
        return f"enrolling in {data.get('class_name', 'a class')}"
    elif op_type == "playtomic_cancel":
        return f"cancelling your Playtomic booking on {_format_date(data['date'])}"
    return "a pending action"


# ──────────────────────────────────────────────
# Playtomic Intent Handlers
# ──────────────────────────────────────────────


def _handle_playtomic_schedule(phone_number: str, entities: dict, sender_name: str) -> str:
    """Handle booking a padel court via Playtomic."""
    date_str = entities.get("date")
    time_str = entities.get("time")
    duration = entities.get("duration_minutes", 90)

    if not date_str:
        return "What date would you like to book a court? (e.g., tomorrow, Saturday, Feb 22)"

    if not time_str:
        return f"What time on {_format_date(date_str)}? (e.g., 5pm, 17:00)"

    # Build datetime
    try:
        start_dt = _build_datetime(date_str, time_str)
    except ValueError as e:
        return f"I couldn't understand that date/time. Could you try again? (Error: {e})"

    now = datetime.now(TZ)
    if start_dt < now:
        return "That time is in the past. Please choose a future date and time."

    # Check Playtomic availability
    try:
        slot = check_court_available(start_dt, time_str, duration)
    except PlaytomicError as e:
        logger.error(f"Playtomic availability check failed: {e}")
        return "I'm having trouble checking Playtomic right now. Please try again in a moment."

    if not slot:
        return _suggest_playtomic_alternatives(start_dt, duration)

    # Set pending operation
    operation_data = {
        "date": date_str,
        "time": time_str,
        "start_dt": start_dt.isoformat(),
        "duration": duration,
        "resource_id": slot["resource_id"],
        "resource_name": slot["resource_name"],
        "price": slot["price"],
        "currency": slot["currency"],
        "student_name": sender_name or entities.get("student_name", ""),
    }
    set_pending_operation(phone_number, "playtomic_book_court", operation_data)

    return (
        f"I found a court for you:\n\n"
        f"  *{slot['resource_name']}*\n"
        f"  {_format_date(date_str)}\n"
        f"  {_format_time(time_str)} ({duration} min)\n\n"
        f"Reply *yes* to reserve or *no* to cancel."
    )


def _handle_playtomic_enroll(phone_number: str, entities: dict) -> str:
    """Handle enrolling in a Playtomic group class."""
    date_str = entities.get("date")

    try:
        classes = pt_get_upcoming_classes()
    except PlaytomicError as e:
        logger.error(f"Playtomic classes fetch failed: {e}")
        return "I'm having trouble checking Playtomic classes right now. Please try again."

    # Filter to group classes only (COURSE), exclude private 1-on-1 lessons
    classes = [c for c in classes if c.get("class_type") == "COURSE"]

    if not classes:
        return "There are no upcoming group classes at the club right now."

    # If date specified, filter
    if date_str:
        classes = [c for c in classes if c["start_time"].strftime("%Y-%m-%d") == date_str]
        if not classes:
            return f"No classes found on {_format_date(date_str)}. Try another date?"

    # Single class found — offer directly
    if len(classes) == 1:
        cls = classes[0]
        duration = cls.get("duration_minutes", 60)
        operation_data = {
            "class_id": cls["class_id"],
            "class_name": cls["name"],
            "start_time": cls["start_time"].isoformat(),
            "end_time": cls["end_time"].isoformat(),
            "duration_minutes": duration,
            "price": cls["price"],
            "currency": cls.get("currency", "EUR"),
        }
        set_pending_operation(phone_number, "playtomic_enroll_class", operation_data)

        return (
            f"Found a class:\n\n"
            f"  *{cls['name']}*\n"
            f"  {cls['start_time'].strftime('%A, %b %d')} at {cls['start_time'].strftime('%I:%M %p')}"
            f" ({duration} min)\n"
            f"  Instructor: {cls.get('instructor', 'TBD')}\n"
            f"  Spots: {cls['spots_available']}/{cls['spots_total']}\n\n"
            f"Reply *yes* to enroll or *no* to skip."
        )

    # Multiple classes: list them
    lines = []
    for i, cls in enumerate(classes[:6], 1):
        duration = cls.get("duration_minutes", 60)
        lines.append(
            f"{i}. *{cls['name']}* - "
            f"{cls['start_time'].strftime('%a %b %d %I:%M %p')} "
            f"({duration}min, {cls['spots_available']} spots)"
        )

    return (
        "Upcoming club classes:\n\n"
        + "\n".join(lines)
        + "\n\nWhich one would you like to join? Reply with the number or class name."
    )


def _handle_playtomic_cancel(phone_number: str, entities: dict) -> str:
    """Handle cancelling a Playtomic booking."""
    date_str = entities.get("date")

    # Check local DB for this user's Playtomic bookings
    if date_str:
        bookings = find_user_playtomic_booking_by_date(phone_number, date_str)
    else:
        bookings = get_user_playtomic_bookings(phone_number)

    if not bookings:
        if date_str:
            return f"I don't see a Playtomic booking for you on {_format_date(date_str)}."
        return "You don't have any upcoming Playtomic bookings to cancel."

    if len(bookings) == 1:
        booking = bookings[0]
        operation_data = {
            "match_id": booking["match_id"],
            "date": booking["start_time"][:10],
            "resource_name": booking.get("resource_name", "Court"),
            "start_time": booking["start_time"],
        }
        set_pending_operation(phone_number, "playtomic_cancel", operation_data)

        start = datetime.fromisoformat(booking["start_time"])
        if start.tzinfo is None:
            start = TZ.localize(start)

        return (
            f"Cancel your Playtomic booking?\n\n"
            f"  {booking.get('resource_name', 'Court')}\n"
            f"  {start.strftime('%A, %b %d')} at {start.strftime('%I:%M %p')}\n\n"
            f"Reply *yes* to confirm or *no* to keep it."
        )

    # Multiple bookings — ask which one
    lines = []
    for i, b in enumerate(bookings[:5], 1):
        start = datetime.fromisoformat(b["start_time"])
        if start.tzinfo is None:
            start = TZ.localize(start)
        lines.append(
            f"{i}. {b.get('resource_name', 'Court')} - "
            f"{start.strftime('%a %b %d')} at {start.strftime('%I:%M %p')}"
        )

    return "Which Playtomic booking would you like to cancel?\n\n" + "\n".join(lines)


def _handle_playtomic_availability(entities: dict) -> str:
    """Show available courts on Playtomic for a given date."""
    date_str = entities.get("date")
    if not date_str:
        return "Which date would you like me to check for courts? (e.g., tomorrow, Saturday)"

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "I couldn't understand that date. Could you try again?"

    try:
        slots = get_court_availability(date_obj)
    except PlaytomicError as e:
        logger.error(f"Playtomic availability check failed: {e}")
        return "I'm having trouble checking Playtomic. Please try again."

    if not slots:
        return f"No courts available on {_format_date(date_str)}. Try another day?"

    # Show up to 8 slots
    lines = []
    for slot in slots[:8]:
        time_fmt = slot["start_time"].strftime("%I:%M %p").lstrip("0")
        lines.append(
            f"  {time_fmt} - {slot['resource_name']} ({slot['duration_minutes']}min)"
        )

    remaining = len(slots) - 8
    response = f"Available courts on *{_format_date(date_str)}*:\n\n" + "\n".join(lines)
    if remaining > 0:
        response += f"\n\n...and {remaining} more slots."
    response += '\n\nWant to book one? Say "book court [time]".'

    return response


def _handle_playtomic_schedule_check(phone_number: str) -> str:
    """Show user's upcoming Playtomic bookings."""
    bookings = get_user_playtomic_bookings(phone_number)
    if not bookings:
        return "You don't have any upcoming Playtomic bookings."

    lines = []
    for b in bookings[:8]:
        start = datetime.fromisoformat(b["start_time"])
        if start.tzinfo is None:
            start = TZ.localize(start)
        lines.append(
            f"  {start.strftime('%a %b %d')} at {start.strftime('%I:%M %p')} - "
            f"{b.get('resource_name', 'Court')} ({b.get('booking_type', 'court')})"
        )

    return "Your Playtomic bookings:\n\n" + "\n".join(lines)


def _handle_combined_schedule_check(phone_number: str) -> str:
    """Show both Calendar and Playtomic schedules."""
    # Calendar
    cal_response = _handle_check_schedule(phone_number)

    # Playtomic
    pt_response = _handle_playtomic_schedule_check(phone_number)

    return (
        "*-- Private Classes (Calendar) --*\n"
        f"{cal_response}\n\n"
        "*-- Playtomic Bookings --*\n"
        f"{pt_response}"
    )


def _handle_cancel_ambiguous(phone_number: str, entities: dict) -> str:
    """When cancel intent has no service specified, check both systems."""
    date_str = entities.get("date")

    # Check both systems
    if date_str:
        cal_events = find_user_event_by_date(phone_number, date_str)
        pt_bookings = find_user_playtomic_booking_by_date(phone_number, date_str)
    else:
        cal_events = get_user_events(phone_number)
        pt_bookings = get_user_playtomic_bookings(phone_number)

    has_cal = bool(cal_events)
    has_pt = bool(pt_bookings)

    if has_cal and not has_pt:
        return _handle_cancel(phone_number, entities)
    elif has_pt and not has_cal:
        entities["service"] = "playtomic"
        return _handle_playtomic_cancel(phone_number, entities)
    elif has_cal and has_pt:
        return (
            "I found bookings in both systems. Which would you like to cancel?\n\n"
            '  Say "cancel my class" for a private class (Calendar)\n'
            '  Say "cancel my court" for a Playtomic booking'
        )
    else:
        return "You don't have any upcoming bookings to cancel."


def _handle_playtomic_reschedule(phone_number: str, entities: dict) -> str:
    """
    Playtomic doesn't support direct reschedule.
    Guide user to cancel + rebook.
    """
    return (
        "Playtomic doesn't support direct rescheduling. "
        "I'd need to cancel your current booking and create a new one.\n\n"
        "Would you like me to:\n"
        '1. Cancel the existing booking (say "cancel my court")\n'
        '2. Then book a new time (say "book court [new time]")\n\n'
        "Note: Cancellation policies may apply."
    )


# ──────────────────────────────────────────────
# Playtomic Pending Operation Executors
# ──────────────────────────────────────────────


def _execute_playtomic_book_court(phone_number: str, data: dict) -> str:
    """Execute a confirmed Playtomic court booking (3-step payment intent)."""
    try:
        start_dt = datetime.fromisoformat(data["start_dt"])
        result = pt_create_booking(
            resource_id=data["resource_id"],
            start_time=start_dt,
            duration_minutes=data["duration"],
        )

        # Calculate end time for DB
        end_dt = start_dt + timedelta(minutes=data["duration"])

        # Save to local DB for tracking
        save_playtomic_booking(
            phone_number=phone_number,
            match_id=result["booking_id"],
            resource_name=data["resource_name"],
            booking_type="court",
            start_time=data["start_dt"],
            end_time=end_dt.isoformat(),
            price=data["price"],
            currency=data["currency"],
        )

        clear_pending_operation(phone_number)

        return (
            f"Court reserved!\n\n"
            f"  *{data['resource_name']}*\n"
            f"  {_format_date(data['date'])}\n"
            f"  {_format_time(data['time'])} ({data['duration']} min)\n\n"
            f"To cancel or change, just message me."
        )
    except PlaytomicSlotTakenError:
        clear_pending_operation(phone_number)
        return (
            "Sorry, that court was just taken by someone else! "
            "Would you like to check what's still available?"
        )
    except PlaytomicBookingError as e:
        logger.error(f"Playtomic booking failed at step {e.step}: {e}")
        clear_pending_operation(phone_number)
        if e.step == "set_payment":
            return "There was a payment issue. Please check your Playtomic account and try again."
        return f"Sorry, the booking failed. Please try again."
    except PlaytomicError as e:
        logger.error(f"Playtomic error: {e}")
        clear_pending_operation(phone_number)
        return "Something went wrong with Playtomic. Please try again."


def _execute_playtomic_enroll_class(phone_number: str, data: dict) -> str:
    """Execute a confirmed Playtomic class enrollment."""
    try:
        result = pt_enroll_in_class(class_id=data["class_id"])

        # Use end_time from class data, or compute from duration
        end_time = data.get("end_time", "")
        if not end_time and data.get("start_time"):
            start_dt = datetime.fromisoformat(data["start_time"])
            duration = data.get("duration_minutes", 60)
            end_time = (start_dt + timedelta(minutes=duration)).isoformat()

        save_playtomic_booking(
            phone_number=phone_number,
            match_id=result["enrollment_id"],
            resource_name=data["class_name"],
            booking_type="class",
            start_time=data["start_time"],
            end_time=end_time,
            price=data["price"],
            currency=data["currency"],
        )

        clear_pending_operation(phone_number)

        start = datetime.fromisoformat(data["start_time"])
        if start.tzinfo is None:
            start = TZ.localize(start)
        duration = data.get("duration_minutes", 60)

        return (
            f"Enrolled!\n\n"
            f"  *{data['class_name']}*\n"
            f"  {start.strftime('%A, %b %d')} at {start.strftime('%I:%M %p')}"
            f" ({duration} min)\n\n"
            f"See you there!"
        )
    except PlaytomicBookingError as e:
        clear_pending_operation(phone_number)
        return f"Enrollment failed: {e}"
    except PlaytomicError as e:
        logger.error(f"Playtomic enrollment error: {e}")
        clear_pending_operation(phone_number)
        return "Something went wrong enrolling you. Please try again."


def _execute_playtomic_cancel(phone_number: str, data: dict) -> str:
    """Execute a confirmed Playtomic booking cancellation."""
    try:
        pt_cancel_booking(data["match_id"])
        delete_playtomic_booking(data["match_id"])
        clear_pending_operation(phone_number)

        return (
            f"Playtomic booking cancelled.\n"
            f"  {data['resource_name']} on {_format_date(data['date'])}\n\n"
            f"Would you like to book another time?"
        )
    except PlaytomicError as e:
        logger.error(f"Playtomic cancel failed: {e}")
        clear_pending_operation(phone_number)
        return (
            "Sorry, I couldn't cancel the booking. The cancellation window may have passed. "
            "Check the Playtomic app directly."
        )


def _suggest_playtomic_alternatives(requested_dt: datetime, duration: int) -> str:
    """Suggest alternative court slots when the requested one is taken."""
    try:
        slots = get_court_availability(requested_dt)
    except PlaytomicError:
        return "That time isn't available, and I'm having trouble finding alternatives. Try a different time?"

    if not slots:
        return (
            f"No courts available on {requested_dt.strftime('%A, %b %d')}. "
            f"Would you like to try a different day?"
        )

    # Show up to 4 alternatives
    lines = []
    for slot in slots[:4]:
        time_fmt = slot["start_time"].strftime("%I:%M %p").lstrip("0")
        lines.append(
            f"  {time_fmt} - {slot['resource_name']} ({slot['duration_minutes']}min)"
        )

    return (
        f"That time is taken. Available courts on "
        f"{requested_dt.strftime('%A, %b %d')}:\n\n"
        + "\n".join(lines)
        + '\n\nWant one of these? Say "book court [time]".'
    )
