"""
Claude-powered intent parser for WhatsApp class scheduling messages.
Extracts intent, entities (date, time, duration, service, sub_type),
and confidence from natural language.

Supports two backend services:
- Google Calendar: private class scheduling
- Playtomic: court bookings and club class enrollment
"""

import os
import json
import logging
from datetime import datetime

import pytz
import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Config
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "America/New_York")

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# System prompt for intent parsing
SYSTEM_PROMPT = """You are a class scheduling and court booking assistant that parses WhatsApp messages.
Your job is to extract structured data from user messages about scheduling classes, booking courts, and managing reservations.

The system supports TWO backends:
1. Google Calendar — for private class scheduling with a teacher
2. Playtomic — for padel/tennis court bookings and club group class enrollment

You MUST return valid JSON only. No explanation, no markdown, just the JSON object.

The JSON schema:
{
    "intent": "schedule | reschedule | cancel | check_availability | check_schedule | book_court | enroll_class | confirm | deny | greeting | unknown",
    "entities": {
        "service": "calendar | playtomic | null",
        "sub_type": "court | class | lesson | null",
        "date": "YYYY-MM-DD or null",
        "time": "HH:MM (24h format) or null",
        "new_date": "YYYY-MM-DD or null (for reschedule)",
        "new_time": "HH:MM or null (for reschedule)",
        "duration_minutes": 60,
        "student_name": "string or null"
    },
    "confidence": 0.0 to 1.0,
    "clarification_needed": true/false,
    "clarification_question": "string or null"
}

Intent rules:
- Convert relative dates ("tomorrow", "next Monday", "this Friday") to absolute dates using today's date
- Convert relative times ("3pm", "afternoon", "morning") to 24h format. "morning" = ask for specific time, "afternoon" = ask for specific time
- For "confirm" intent: user says yes/ok/sure/confirm/dale/si/vale/venga
- For "deny" intent: user says no/cancel/nope/nevermind/na/nah
- For greetings: hi/hello/hey/hola/buenos dias/buenas etc.
- If the message is ambiguous (e.g. "I want a class" without date/time), set clarification_needed=true and provide a clarification_question
- If you can partially parse (got date but no time), include what you have and ask for the rest
- For reschedule: try to identify both the original event (date/time) and the new desired slot (new_date/new_time)
- Confidence should reflect how certain you are about the intent AND entities

Service detection rules:
- "service" determines which backend system handles the request:
  - "calendar": Private class scheduling via Google Calendar. Default for "my class", "lesson with teacher", "private lesson", "clase privada", "clase particular"
  - "playtomic": Court bookings and club activities via Playtomic. For "court", "pista", "cancha", "padel court", "club class", "group class", "clase grupal"
  - null: When ambiguous — set clarification_needed=true and ask "Are you looking to book a padel court on Playtomic, or schedule a private class?"
- "sub_type" specifies what kind of booking:
  - "court": Padel/tennis court booking (always service=playtomic)
  - "class": Group class (default to "playtomic" if "club class" / "group class" / "clase grupal"; otherwise ask)
  - "lesson": Private lesson (default to calendar)
  - null: When not applicable or not specified
- Keywords that signal Playtomic: court, pista, cancha, padel, reserve a court, reservar pista, club class, group class, clase grupal, playtomic, partido
- Keywords that signal Calendar: private class, my class, lesson, teacher, profesor, clase privada, clase particular
- "book_court" intent: user explicitly wants to reserve a padel/tennis court (always service=playtomic)
- "enroll_class" intent: user explicitly wants to join a group class at the club (always service=playtomic)
- Default duration: 90 minutes for courts (padel standard), 60 minutes for classes/lessons
- When user says "check availability" with court context -> service=playtomic; without context -> service=null (check both)
- When user says "cancel" or "my schedule" without specifying which service -> set service=null so handler checks both systems
- When user says "show classes" or "que clases hay" without qualifier -> service=playtomic (they mean club schedule)
"""


def parse_user_message(
    message_text: str,
    conversation_history: list = None,
    current_datetime: datetime = None,
) -> dict:
    """
    Parse a user's WhatsApp message to extract scheduling intent and entities.

    Args:
        message_text: The raw message text from the user
        conversation_history: Optional list of previous messages for context
            Format: [{"role": "user"|"assistant", "content": "..."}]
        current_datetime: Current datetime for relative date resolution (defaults to now)

    Returns:
        Dict with intent, entities, confidence, clarification_needed, clarification_question
    """
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "your_anthropic_api_key":
        logger.error("ANTHROPIC_API_KEY not configured")
        return _fallback_parse(message_text)

    tz = pytz.timezone(CALENDAR_TIMEZONE)
    if current_datetime is None:
        current_datetime = datetime.now(tz)

    # Build context message
    context = f"Today is {current_datetime.strftime('%A, %B %d, %Y')}. "
    context += f"Current time is {current_datetime.strftime('%I:%M %p')}. "
    context += f"Timezone is {CALENDAR_TIMEZONE}."

    # Build messages array
    messages = []

    # Add conversation history for context (last 6 messages max)
    if conversation_history:
        for msg in conversation_history[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current message
    messages.append({
        "role": "user",
        "content": f"{context}\n\nUser message: \"{message_text}\"\n\nParse this message and return JSON only.",
    })

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        # Extract text from response
        response_text = response.content[0].text.strip()

        # Parse JSON (handle potential markdown code blocks)
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        result = json.loads(response_text)

        # Validate required fields
        result.setdefault("intent", "unknown")
        result.setdefault("entities", {})
        result.setdefault("confidence", 0.5)
        result.setdefault("clarification_needed", False)
        result.setdefault("clarification_question", None)
        result["entities"].setdefault("duration_minutes", 60)
        result["entities"].setdefault("service", None)
        result["entities"].setdefault("sub_type", None)

        # Override duration default for court bookings
        if result["entities"].get("sub_type") == "court":
            if result["entities"]["duration_minutes"] == 60:
                result["entities"]["duration_minutes"] = 90

        logger.info(
            f"Parsed intent: {result['intent']} | service: {result['entities'].get('service')} | "
            f"sub_type: {result['entities'].get('sub_type')} | confidence: {result['confidence']}"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude response as JSON: {e}")
        return _fallback_parse(message_text)
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return _fallback_parse(message_text)
    except Exception as e:
        logger.error(f"Unexpected error in intent parsing: {e}")
        return _fallback_parse(message_text)


def _fallback_parse(message_text: str) -> dict:
    """
    Simple keyword-based fallback when Claude API is unavailable.
    Not as smart, but keeps the bot functional.
    """
    text = message_text.lower().strip()

    # Default entity dict
    base_entities = {
        "duration_minutes": 60,
        "service": None,
        "sub_type": None,
    }

    # Confirmation/denial
    if text in ("yes", "si", "sí", "ok", "sure", "confirm", "dale", "ya", "yep", "yeah", "vale", "venga"):
        return {
            "intent": "confirm",
            "entities": base_entities.copy(),
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    if text in ("no", "nope", "nevermind", "na", "nah"):
        return {
            "intent": "deny",
            "entities": base_entities.copy(),
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    # Greeting
    if text in ("hi", "hello", "hey", "hola", "buenos dias", "buenas"):
        return {
            "intent": "greeting",
            "entities": base_entities.copy(),
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    # ── Service detection (Playtomic vs Calendar) ──
    service = None
    sub_type = None

    if any(w in text for w in ("court", "pista", "cancha", "padel court", "padel")):
        service = "playtomic"
        sub_type = "court"
    elif any(w in text for w in ("group class", "club class", "clase grupal", "enroll", "sign up", "apuntarme")):
        service = "playtomic"
        sub_type = "class"
    elif any(w in text for w in ("playtomic",)):
        service = "playtomic"
    elif any(w in text for w in ("private class", "clase privada", "clase particular", "lesson", "teacher", "profesor")):
        service = "calendar"
        sub_type = "lesson"

    # ── Intent detection by keywords ──
    # Order matters: check cancel/reschedule BEFORE schedule/book to avoid
    # "cancel my booking" matching "book" as schedule intent.
    intent = "unknown"

    if any(w in text for w in ("cancel", "delete", "remove", "cancelar", "eliminar")):
        intent = "cancel"
    elif any(w in text for w in ("reschedule", "move", "change", "cambiar", "mover")):
        intent = "reschedule"
    elif sub_type == "court" and any(w in text for w in ("book", "reserve", "reservar", "schedule", "agendar")):
        intent = "book_court"
    elif sub_type == "class" and any(w in text for w in ("enroll", "sign up", "join", "apuntarme", "inscribir")):
        intent = "enroll_class"
    elif any(w in text for w in ("upcoming", "my classes", "mis clases", "agenda", "my bookings", "mis reservas", "my schedule", "show schedule", "show my")):
        intent = "check_schedule"
    elif any(w in text for w in ("schedule", "book", "reserve", "agendar", "reservar")):
        intent = "schedule" if sub_type != "court" else "book_court"
    elif any(w in text for w in ("available", "free", "open", "disponible", "availability")):
        intent = "check_availability"
    elif any(w in text for w in ("classes", "clases", "que hay")):
        intent = "enroll_class"
        service = service or "playtomic"
        sub_type = sub_type or "class"

    # Set duration based on sub_type
    duration = 90 if sub_type == "court" else 60

    # If we matched a known intent, give reasonable confidence so the
    # conversation handler routes it instead of asking for clarification.
    # Only truly unknown intents should trigger clarification.
    if intent != "unknown":
        return {
            "intent": intent,
            "entities": {
                "duration_minutes": duration,
                "service": service,
                "sub_type": sub_type,
            },
            "confidence": 0.75,
            "clarification_needed": False,
            "clarification_question": None,
        }

    return {
        "intent": intent,
        "entities": {
            "duration_minutes": duration,
            "service": service,
            "sub_type": sub_type,
        },
        "confidence": 0.3,
        "clarification_needed": True,
        "clarification_question": (
            "I'm not sure what you need. Here are some things I can help with:\n\n"
            "*Private classes:*\n"
            '  "Schedule a class tomorrow at 3pm"\n\n'
            "*Playtomic (courts & club):*\n"
            '  "Book a court Saturday at 5pm"\n'
            '  "Show me group classes"\n'
            '  "Show my schedule"'
        ),
    }
