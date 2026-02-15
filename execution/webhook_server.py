"""
Flask webhook server for WhatsApp Cloud API.
Handles webhook verification (GET) and incoming messages (POST).
"""

import os
import sys
import logging
from pathlib import Path

from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from execution.whatsapp_api import parse_webhook_payload, send_message, mark_as_read
from execution.conversation_handler import handle_incoming_message
from execution.conversation_state import init_db

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / ".tmp" / "bot_operations.log"),
    ],
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)

# Config
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    WhatsApp webhook verification endpoint.
    Meta sends a GET request with hub.mode, hub.verify_token, and hub.challenge.
    We verify the token matches and return the challenge.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully.")
        return challenge, 200

    logger.warning(f"Webhook verification failed. mode={mode}, token={token}")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """
    Receive incoming WhatsApp messages.
    Parses the payload, processes the message, and sends a reply.
    """
    try:
        payload = request.json

        # Parse the webhook payload
        message_data = parse_webhook_payload(payload)

        # Ignore non-text messages or status updates
        if not message_data:
            return jsonify({"status": "ignored"}), 200

        if message_data["message_type"] not in ("text", "interactive"):
            logger.info(f"Ignoring {message_data['message_type']} message")
            send_message(
                message_data["phone_number"],
                "I can only process text messages right now. Please type your request."
            )
            return jsonify({"status": "ignored_type"}), 200

        logger.info(
            f"Message from {message_data['phone_number']}: {message_data['message_text'][:100]}"
        )

        # Mark as read immediately (shows blue checkmarks)
        mark_as_read(message_data["message_id"])

        # Process the message through conversation handler
        response_text = handle_incoming_message(
            phone_number=message_data["phone_number"],
            message_text=message_data["message_text"],
            sender_name=message_data.get("sender_name", ""),
        )

        # Send the reply
        if response_text:
            send_message(message_data["phone_number"], response_text)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        # Always return 200 to WhatsApp (prevent retries for app errors)
        return jsonify({"status": "error"}), 200


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for deployment platforms (Railway, Render)."""
    return jsonify({"status": "healthy", "service": "whatsapp-class-scheduler"}), 200


@app.route("/", methods=["GET"])
def index():
    """Root endpoint."""
    return jsonify({
        "service": "WhatsApp Class Scheduling Agent",
        "status": "running",
        "endpoints": {
            "/webhook": "WhatsApp webhook (GET: verify, POST: messages)",
            "/health": "Health check",
        },
    }), 200


def create_app():
    """App factory for gunicorn."""
    # Ensure .tmp directory exists for logs and DB
    (PROJECT_ROOT / ".tmp").mkdir(parents=True, exist_ok=True)

    # Initialize database
    init_db()

    return app


if __name__ == "__main__":
    # Ensure .tmp directory exists
    (PROJECT_ROOT / ".tmp").mkdir(parents=True, exist_ok=True)

    # Initialize database
    init_db()

    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
