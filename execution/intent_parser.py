"""
Claude-powered intent parser for WhatsApp class scheduling messages.
Extracts intent, entities (date, time, duration), and confidence from natural language.
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
SYSTEM_PROMPT = """You are a class scheduling assistant that parses WhatsApp messages.
Your job is to extract structured data from user messages about scheduling, rescheduling, or cancelling classes.

You MUST return valid JSON only. No explanation, no markdown, just the JSON object.

The JSON schema:
{
    "intent": "schedule | reschedule | cancel | check_availability | check_schedule | confirm | deny | greeting | unknown",
    "entities": {
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

Rules:
- Convert relative dates ("tomorrow", "next Monday", "this Friday") to absolute dates using today's date
- Convert relative times ("3pm", "afternoon", "morning") to 24h format. "morning" = ask for specific time, "afternoon" = ask for specific time
- Default duration is 60 minutes unless specified
- For "confirm" intent: user says yes/ok/sure/confirm/dale/si
- For "deny" intent: user says no/cancel/nope/nevermind/na
- For greetings: hi/hello/hey/hola/buenos dias etc.
- If the message is ambiguous (e.g. "I want a class" without date/time), set clarification_needed=true and provide a clarification_question
- If you can partially parse (got date but no time), include what you have and ask for the rest
- For reschedule: try to identify both the original event (date/time) and the new desired slot (new_date/new_time)
- Confidence should reflect how certain you are about the intent AND entities
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

        logger.info(f"Parsed intent: {result['intent']} (confidence: {result['confidence']})")
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

    # Confirmation/denial
    if text in ("yes", "si", "sí", "ok", "sure", "confirm", "dale", "ya", "yep", "yeah"):
        return {
            "intent": "confirm",
            "entities": {"duration_minutes": 60},
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    if text in ("no", "nope", "cancel", "nevermind", "na", "nah"):
        return {
            "intent": "deny",
            "entities": {"duration_minutes": 60},
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    # Greeting
    if text in ("hi", "hello", "hey", "hola", "buenos dias", "buenas"):
        return {
            "intent": "greeting",
            "entities": {"duration_minutes": 60},
            "confidence": 0.95,
            "clarification_needed": False,
            "clarification_question": None,
        }

    # Intent detection by keywords
    intent = "unknown"
    if any(w in text for w in ("schedule", "book", "reserve", "agendar", "reservar")):
        intent = "schedule"
    elif any(w in text for w in ("reschedule", "move", "change", "cambiar", "mover")):
        intent = "reschedule"
    elif any(w in text for w in ("cancel", "delete", "remove", "cancelar", "eliminar")):
        intent = "cancel"
    elif any(w in text for w in ("available", "free", "open", "disponible")):
        intent = "check_availability"
    elif any(w in text for w in ("schedule", "upcoming", "my classes", "mis clases", "agenda")):
        intent = "check_schedule"

    return {
        "intent": intent,
        "entities": {"duration_minutes": 60},
        "confidence": 0.3,
        "clarification_needed": True,
        "clarification_question": "I'm having trouble understanding. Could you tell me what you'd like to do? (schedule a class, reschedule, cancel, or check availability)",
    }
