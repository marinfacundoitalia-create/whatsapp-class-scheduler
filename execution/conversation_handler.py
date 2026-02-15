"""
Core conversation orchestrator for the WhatsApp Class Scheduling Agent.
Routes parsed intents to calendar actions, manages confirmation flows, and formats replies.
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
        response = _handle_schedule(phone_number, entities, sender_name)
    elif intent == "reschedule":
        response = _handle_reschedule(phone_number, entities)
    elif intent == "cancel":
        response = _handle_cancel(phone_number, entities)
    elif intent == "check_availability":
        response = _handle_check_availability(entities)
    elif intent == "check_schedule":
        response = _handle_check_schedule(phone_number)
    elif intent == "greeting":
        response = _handle_greeting(sender_name, phone_number)
    else:
        response = (
            "I can help you with:\n"
            '• *Schedule* a class — "Book a class tomorrow at 3pm"\n'
            '• *Reschedule* — "Move my Monday class to Tuesday 4pm"\n'
            '• *Cancel* — "Cancel my Friday class"\n'
            '• *Check availability* — "What\'s available next Monday?"\n'
            '• *My schedule* — "Show my upcoming classes"'
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
            f"Hi {name}! 👋 What would you like to do?\n\n"
            "As the owner, you can:\n"
            '• "Show all bookings today"\n'
            '• "Block out time tomorrow 2-4pm"\n'
            '• Or any regular scheduling command'
        )

    return (
        f"Hi {name}! 👋 I'm the class scheduling assistant.\n\n"
        "I can help you:\n"
        '• *Schedule* a class — "Book a class Friday at 2pm"\n'
        '• *Reschedule* — "Move my class to next week"\n'
        '• *Cancel* — "Cancel my upcoming class"\n'
        '• *Check times* — "What\'s available this week?"'
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
    return "a pending action"
