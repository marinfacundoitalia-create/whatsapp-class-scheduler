"""
WhatsApp Cloud API client.
Handles sending messages, marking as read, and parsing webhook payloads.
"""

import os
import logging

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Config from .env
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")

BASE_URL = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}


def send_message(to_phone_number: str, message_text: str) -> dict:
    """
    Send a text message via WhatsApp Cloud API.

    Args:
        to_phone_number: Recipient phone number (with country code, no +)
        message_text: The message to send

    Returns:
        API response dict
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_number,
        "type": "text",
        "text": {"body": message_text},
    }

    try:
        response = requests.post(BASE_URL, headers=HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        logger.info(f"Message sent to {to_phone_number}: {message_text[:50]}...")
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send message to {to_phone_number}: {e}")
        raise


def send_template_message(
    to_phone_number: str,
    template_name: str = "hello_world",
    language_code: str = "en_US",
    parameters: list = None,
) -> dict:
    """
    Send a pre-approved template message (for outside 24h window).

    Args:
        to_phone_number: Recipient phone number
        template_name: Approved template name
        language_code: Template language code
        parameters: Optional list of parameter values for the template

    Returns:
        API response dict
    """
    template = {
        "name": template_name,
        "language": {"code": language_code},
    }

    if parameters:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in parameters],
            }
        ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_number,
        "type": "template",
        "template": template,
    }

    try:
        response = requests.post(BASE_URL, headers=HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        logger.info(f"Template message sent to {to_phone_number}: {template_name}")
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send template to {to_phone_number}: {e}")
        raise


def mark_as_read(message_id: str) -> bool:
    """
    Mark a message as read (shows blue checkmarks to sender).

    Args:
        message_id: The WhatsApp message ID

    Returns:
        True if successful
    """
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    try:
        response = requests.post(BASE_URL, headers=HEADERS, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to mark message as read: {e}")
        return False


def parse_webhook_payload(payload: dict) -> dict:
    """
    Extract message data from a WhatsApp webhook POST payload.

    Args:
        payload: The raw JSON payload from the webhook

    Returns:
        Dict with phone_number, message_id, message_text, timestamp, message_type.
        Returns None if payload doesn't contain a user message.
    """
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})

        # Check if this is a message (not a status update)
        messages = value.get("messages")
        if not messages:
            return None

        message = messages[0]
        contacts = value.get("contacts", [{}])

        # Extract sender info
        phone_number = message.get("from", "")
        message_id = message.get("id", "")
        timestamp = message.get("timestamp", "")
        message_type = message.get("type", "unknown")

        # Get sender name
        sender_name = ""
        if contacts:
            sender_name = contacts[0].get("profile", {}).get("name", "")

        # Extract text content
        message_text = ""
        if message_type == "text":
            message_text = message.get("text", {}).get("body", "")
        elif message_type == "interactive":
            # Handle button replies or list replies
            interactive = message.get("interactive", {})
            if interactive.get("type") == "button_reply":
                message_text = interactive.get("button_reply", {}).get("title", "")
            elif interactive.get("type") == "list_reply":
                message_text = interactive.get("list_reply", {}).get("title", "")

        return {
            "phone_number": phone_number,
            "message_id": message_id,
            "message_text": message_text,
            "message_type": message_type,
            "timestamp": timestamp,
            "sender_name": sender_name,
        }

    except (IndexError, KeyError, TypeError) as e:
        logger.error(f"Error parsing webhook payload: {e}")
        return None
