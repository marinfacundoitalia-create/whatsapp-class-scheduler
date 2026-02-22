"""
Conversation state management using SQLite.
Tracks conversation history, pending operations, and event-to-user mapping.
Database stored in .tmp/conversations.db (gitignored, regenerable).
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Database path
PROJECT_ROOT = Path(__file__).parent.parent
DB_DIR = PROJECT_ROOT / ".tmp"
DB_PATH = DB_DIR / "conversations.db"


def _get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
    return conn


def init_db():
    """Create database tables if they don't exist."""
    conn = _get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone_number TEXT PRIMARY KEY,
                last_message_time TEXT NOT NULL,
                conversation_history TEXT DEFAULT '[]',
                pending_operation TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                google_event_id TEXT NOT NULL UNIQUE,
                summary TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_phone
                ON scheduled_events(phone_number);

            CREATE INDEX IF NOT EXISTS idx_events_google_id
                ON scheduled_events(google_event_id);

            CREATE TABLE IF NOT EXISTS playtomic_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                match_id TEXT NOT NULL UNIQUE,
                resource_name TEXT,
                booking_type TEXT NOT NULL DEFAULT 'court',
                start_time TEXT NOT NULL,
                end_time TEXT,
                price REAL DEFAULT 0,
                currency TEXT DEFAULT 'EUR',
                status TEXT DEFAULT 'CONFIRMED',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pt_bookings_phone
                ON playtomic_bookings(phone_number);

            CREATE INDEX IF NOT EXISTS idx_pt_bookings_match
                ON playtomic_bookings(match_id);

            CREATE INDEX IF NOT EXISTS idx_pt_bookings_status
                ON playtomic_bookings(status);
        """)
        conn.commit()
        logger.info("Database initialized successfully.")
    finally:
        conn.close()


# ──────────────────────────────────────────────
# Conversation CRUD
# ──────────────────────────────────────────────

def get_conversation(phone_number: str) -> dict:
    """
    Retrieve conversation state for a user.

    Returns:
        Dict with phone_number, last_message_time, conversation_history (list),
        pending_operation (dict or None). Returns None if user doesn't exist.
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM conversations WHERE phone_number = ?",
            (phone_number,)
        ).fetchone()

        if not row:
            return None

        return {
            "phone_number": row["phone_number"],
            "last_message_time": row["last_message_time"],
            "conversation_history": json.loads(row["conversation_history"]),
            "pending_operation": json.loads(row["pending_operation"]) if row["pending_operation"] else None,
        }
    finally:
        conn.close()


def update_conversation(phone_number: str, role: str, message: str):
    """
    Add a message to conversation history and update last_message_time.

    Args:
        phone_number: User's WhatsApp number
        role: "user" or "assistant"
        message: The message text
    """
    now = datetime.utcnow().isoformat()
    conn = _get_connection()

    try:
        existing = conn.execute(
            "SELECT conversation_history FROM conversations WHERE phone_number = ?",
            (phone_number,)
        ).fetchone()

        if existing:
            history = json.loads(existing["conversation_history"])
        else:
            history = []

        # Add new message
        history.append({"role": role, "content": message, "timestamp": now})

        # Keep only last 20 messages to avoid bloat
        if len(history) > 20:
            history = history[-20:]

        history_json = json.dumps(history)

        if existing:
            conn.execute(
                """UPDATE conversations
                   SET conversation_history = ?, last_message_time = ?, updated_at = ?
                   WHERE phone_number = ?""",
                (history_json, now, now, phone_number),
            )
        else:
            conn.execute(
                """INSERT INTO conversations
                   (phone_number, last_message_time, conversation_history, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (phone_number, now, history_json, now, now),
            )

        conn.commit()
    finally:
        conn.close()


def get_conversation_history(phone_number: str) -> list:
    """Get conversation history as a list of message dicts."""
    conv = get_conversation(phone_number)
    if conv:
        return conv["conversation_history"]
    return []


# ──────────────────────────────────────────────
# Pending Operations
# ──────────────────────────────────────────────

def set_pending_operation(phone_number: str, operation_type: str, operation_data: dict):
    """
    Store a pending operation awaiting user confirmation.

    Args:
        phone_number: User's WhatsApp number
        operation_type: "schedule", "reschedule", or "cancel"
        operation_data: Dict with operation details (date, time, event_id, etc.)
    """
    now = datetime.utcnow().isoformat()
    pending = json.dumps({
        "type": operation_type,
        "data": operation_data,
        "created_at": now,
    })

    conn = _get_connection()
    try:
        existing = conn.execute(
            "SELECT 1 FROM conversations WHERE phone_number = ?",
            (phone_number,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE conversations SET pending_operation = ?, updated_at = ? WHERE phone_number = ?",
                (pending, now, phone_number),
            )
        else:
            conn.execute(
                """INSERT INTO conversations
                   (phone_number, last_message_time, pending_operation, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (phone_number, now, pending, now, now),
            )

        conn.commit()
        logger.info(f"Pending operation set for {phone_number}: {operation_type}")
    finally:
        conn.close()


def get_pending_operation(phone_number: str) -> dict:
    """Get the pending operation for a user, or None."""
    conv = get_conversation(phone_number)
    if conv:
        return conv["pending_operation"]
    return None


def clear_pending_operation(phone_number: str):
    """Clear the pending operation after confirmation or cancellation."""
    now = datetime.utcnow().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE conversations SET pending_operation = NULL, updated_at = ? WHERE phone_number = ?",
            (now, phone_number),
        )
        conn.commit()
        logger.info(f"Pending operation cleared for {phone_number}")
    finally:
        conn.close()


# ──────────────────────────────────────────────
# Scheduled Events (WhatsApp user ↔ Calendar event mapping)
# ──────────────────────────────────────────────

def save_scheduled_event(
    phone_number: str,
    google_event_id: str,
    summary: str,
    start_time: str,
    end_time: str,
):
    """
    Link a WhatsApp user to a Google Calendar event.

    Args:
        phone_number: User's WhatsApp number
        google_event_id: Google Calendar event ID
        summary: Event title
        start_time: ISO format start time
        end_time: ISO format end time
    """
    now = datetime.utcnow().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO scheduled_events
               (phone_number, google_event_id, summary, start_time, end_time, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (phone_number, google_event_id, summary, start_time, end_time, now),
        )
        conn.commit()
        logger.info(f"Event {google_event_id} saved for {phone_number}")
    finally:
        conn.close()


def get_user_events(phone_number: str) -> list:
    """Get all scheduled events for a user."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM scheduled_events
               WHERE phone_number = ?
               ORDER BY start_time ASC""",
            (phone_number,),
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_event_by_google_id(google_event_id: str) -> dict:
    """Find a scheduled event by its Google Calendar ID."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM scheduled_events WHERE google_event_id = ?",
            (google_event_id,),
        ).fetchone()

        return dict(row) if row else None
    finally:
        conn.close()


def delete_scheduled_event(google_event_id: str):
    """Remove event-to-user mapping after cancellation."""
    conn = _get_connection()
    try:
        conn.execute(
            "DELETE FROM scheduled_events WHERE google_event_id = ?",
            (google_event_id,),
        )
        conn.commit()
        logger.info(f"Event {google_event_id} removed from DB")
    finally:
        conn.close()


def find_user_event_by_date(phone_number: str, date_str: str) -> list:
    """
    Find a user's events on a specific date.

    Args:
        phone_number: User's WhatsApp number
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of matching event dicts.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM scheduled_events
               WHERE phone_number = ?
               AND start_time LIKE ?
               ORDER BY start_time ASC""",
            (phone_number, f"{date_str}%"),
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 24-Hour Window Check
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# Playtomic Bookings (WhatsApp user ↔ Playtomic match mapping)
# ──────────────────────────────────────────────

def save_playtomic_booking(
    phone_number: str,
    match_id: str,
    resource_name: str,
    booking_type: str,
    start_time: str,
    end_time: str = "",
    price: float = 0,
    currency: str = "EUR",
):
    """
    Link a WhatsApp user to a Playtomic booking.

    Args:
        phone_number: User's WhatsApp number
        match_id: Playtomic match/booking UUID
        resource_name: Court name or class name
        booking_type: "court" or "class"
        start_time: ISO format start time
        end_time: ISO format end time
        price: Booking price
        currency: Currency code (default EUR)
    """
    now = datetime.utcnow().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO playtomic_bookings
               (phone_number, match_id, resource_name, booking_type,
                start_time, end_time, price, currency, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?)""",
            (phone_number, match_id, resource_name, booking_type,
             start_time, end_time, price, currency, now),
        )
        conn.commit()
        logger.info(f"Playtomic booking {match_id} saved for {phone_number}")
    finally:
        conn.close()


def get_user_playtomic_bookings(phone_number: str) -> list:
    """Get all active (confirmed, future) Playtomic bookings for a user."""
    now = datetime.utcnow().isoformat()
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM playtomic_bookings
               WHERE phone_number = ?
               AND status = 'CONFIRMED'
               AND start_time > ?
               ORDER BY start_time ASC""",
            (phone_number, now),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def find_user_playtomic_booking_by_date(phone_number: str, date_str: str) -> list:
    """
    Find a user's Playtomic bookings on a specific date.

    Args:
        phone_number: User's WhatsApp number
        date_str: Date in YYYY-MM-DD format

    Returns:
        List of matching booking dicts.
    """
    conn = _get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM playtomic_bookings
               WHERE phone_number = ?
               AND status = 'CONFIRMED'
               AND start_time LIKE ?
               ORDER BY start_time ASC""",
            (phone_number, f"{date_str}%"),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_playtomic_booking(match_id: str):
    """Mark a Playtomic booking as cancelled (soft delete for audit trail)."""
    now = datetime.utcnow().isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            """UPDATE playtomic_bookings
               SET status = 'CANCELLED'
               WHERE match_id = ?""",
            (match_id,),
        )
        conn.commit()
        logger.info(f"Playtomic booking {match_id} marked as cancelled")
    finally:
        conn.close()


def get_playtomic_booking_by_match_id(match_id: str) -> dict:
    """Find a Playtomic booking by its match ID."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM playtomic_bookings WHERE match_id = ?",
            (match_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 24-Hour Window Check
# ──────────────────────────────────────────────

def is_within_24h_window(phone_number: str) -> bool:
    """
    Check if the user's last message was within the 24-hour WhatsApp window.
    If True, we can send free-form messages. If False, must use templates.
    """
    conv = get_conversation(phone_number)
    if not conv:
        return False

    try:
        last_time = datetime.fromisoformat(conv["last_message_time"])
        return (datetime.utcnow() - last_time) < timedelta(hours=24)
    except (ValueError, TypeError):
        return False
