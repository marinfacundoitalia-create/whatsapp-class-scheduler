"""
Playtomic API client for court bookings and class enrollment.
Handles authentication (email/password + JWT refresh), court availability,
booking flow (3-step payment intent), class management, and cancellation.

NOTE: This uses the reverse-engineered consumer API (not the official read-only Club API).
Endpoints may change without notice. All parsing is defensive (.get() with defaults).
"""

import os
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
import pytz
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logger = logging.getLogger(__name__)

# ── Config from .env ──
PLAYTOMIC_EMAIL = os.getenv("PLAYTOMIC_EMAIL")
PLAYTOMIC_PASSWORD = os.getenv("PLAYTOMIC_PASSWORD")
PLAYTOMIC_TENANT_ID = os.getenv("PLAYTOMIC_TENANT_ID")
PLAYTOMIC_SPORT_ID = os.getenv("PLAYTOMIC_SPORT_ID", "PADEL")
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "America/New_York")

BASE_URL = "https://api.playtomic.io/v1"

# Auth endpoint — reverse-engineered from Playtomic web/mobile app
AUTH_URL = "https://api.playtomic.io/v3/auth/login"

# Default request headers (mimic the Playtomic mobile app)
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Language": "en",
    "User-Agent": "Playtomic/1.0",
}

# ── In-memory token storage (single owner account) ──
_token_store = {
    "access_token": None,
    "refresh_token": None,
    "user_id": None,
    "expires_at": 0,  # Unix timestamp
}

# Rate limiting: minimum seconds between API calls
_RATE_LIMIT_DELAY = 0.5
_last_api_call = 0

# ── Resource name cache (resource_id -> name) ──
_resource_names = {}


# ══════════════════════════════════════════════
# Custom Exceptions
# ══════════════════════════════════════════════

class PlaytomicError(Exception):
    """Base exception for Playtomic API errors."""
    pass


class PlaytomicAuthError(PlaytomicError):
    """Authentication or token refresh errors."""
    pass


class PlaytomicAPIError(PlaytomicError):
    """General API errors (network, bad response, unexpected format)."""
    pass


class PlaytomicBookingError(PlaytomicError):
    """Booking-specific errors (slot taken, payment failed, class full)."""
    def __init__(self, message: str, step: str = None):
        super().__init__(message)
        self.step = step  # "create_intent", "set_payment", "confirm"


class PlaytomicSlotTakenError(PlaytomicBookingError):
    """The requested slot was taken between availability check and booking."""
    def __init__(self, message: str = "Slot already taken"):
        super().__init__(message, step="create_intent")


# ══════════════════════════════════════════════
# Rate Limiting Helper
# ══════════════════════════════════════════════

def _rate_limit():
    """Enforce minimum delay between API calls."""
    global _last_api_call
    now = time.time()
    elapsed = now - _last_api_call
    if elapsed < _RATE_LIMIT_DELAY:
        time.sleep(_RATE_LIMIT_DELAY - elapsed)
    _last_api_call = time.time()


# ══════════════════════════════════════════════
# Resource Name Cache
# ══════════════════════════════════════════════

def _load_resource_names():
    """
    Fetch resource (court) names from the tenant endpoint and cache them.
    Called lazily on first availability check. No auth required.
    """
    global _resource_names
    if _resource_names:
        return  # Already loaded

    try:
        resp = requests.get(
            f"{BASE_URL}/tenants/{PLAYTOMIC_TENANT_ID}",
            headers=DEFAULT_HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for resource in data.get("resources", []):
                rid = resource.get("resource_id") or resource.get("id")
                name = resource.get("name", "Court")
                if rid:
                    _resource_names[rid] = name
            logger.info(f"Loaded {len(_resource_names)} resource names from tenant info.")
    except Exception as e:
        logger.warning(f"Could not load resource names: {e}")


def _get_resource_name(resource_id: str) -> str:
    """Get the human-readable name for a resource, loading cache if needed."""
    if not _resource_names:
        _load_resource_names()
    return _resource_names.get(resource_id, "Court")


# ══════════════════════════════════════════════
# Authentication
# ══════════════════════════════════════════════

def authenticate() -> str:
    """
    Get a valid access token. Auto-refreshes if expired.
    Uses email/password login on first call, then refresh_token thereafter.

    Returns:
        Valid access token string.

    Raises:
        PlaytomicAuthError: If login and refresh both fail.
    """
    now = time.time()

    # Token still valid (with 60s safety buffer)
    if _token_store["access_token"] and _token_store["expires_at"] > now + 60:
        return _token_store["access_token"]

    # Try refresh first if we have a refresh token
    if _token_store["refresh_token"]:
        try:
            _refresh_access_token()
            logger.info("Playtomic token refreshed successfully.")
            return _token_store["access_token"]
        except PlaytomicAuthError:
            logger.warning("Playtomic token refresh failed, falling back to login.")

    # Full login
    _login()
    return _token_store["access_token"]


def _login() -> dict:
    """
    Login with email/password. Stores tokens in _token_store.

    Raises:
        PlaytomicAuthError: On 401/403 or network error.
    """
    if not PLAYTOMIC_EMAIL or not PLAYTOMIC_PASSWORD:
        raise PlaytomicAuthError(
            "PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD must be set in .env"
        )

    _rate_limit()

    try:
        resp = requests.post(
            AUTH_URL,
            json={
                "email": PLAYTOMIC_EMAIL,
                "password": PLAYTOMIC_PASSWORD,
            },
            headers=DEFAULT_HEADERS,
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicAuthError(f"Network error during login: {e}")

    if resp.status_code in (401, 403):
        raise PlaytomicAuthError("Invalid Playtomic credentials. Check PLAYTOMIC_EMAIL and PLAYTOMIC_PASSWORD.")

    if resp.status_code != 200:
        raise PlaytomicAuthError(f"Playtomic login failed with status {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    _token_store["access_token"] = data.get("access_token") or data.get("accessToken")
    _token_store["refresh_token"] = data.get("refresh_token") or data.get("refreshToken")
    _token_store["user_id"] = data.get("user_id") or data.get("userId")

    # Calculate expiry: default to 1 hour if not provided
    expires_in = data.get("expires_in") or data.get("expiresIn") or 3600
    _token_store["expires_at"] = time.time() + expires_in

    if not _token_store["access_token"]:
        raise PlaytomicAuthError(f"No access_token in login response: {list(data.keys())}")

    logger.info(f"Playtomic login successful. User ID: {_token_store['user_id']}")
    return data


def _refresh_access_token() -> dict:
    """
    Refresh the access token using the stored refresh_token.

    Raises:
        PlaytomicAuthError: On failure.
    """
    _rate_limit()

    try:
        resp = requests.post(
            AUTH_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": _token_store["refresh_token"],
            },
            headers=DEFAULT_HEADERS,
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicAuthError(f"Network error during token refresh: {e}")

    if resp.status_code != 200:
        raise PlaytomicAuthError(f"Token refresh failed: {resp.status_code}")

    data = resp.json()
    _token_store["access_token"] = data.get("access_token") or data.get("accessToken")
    if data.get("refresh_token") or data.get("refreshToken"):
        _token_store["refresh_token"] = data.get("refresh_token") or data.get("refreshToken")

    expires_in = data.get("expires_in") or data.get("expiresIn") or 3600
    _token_store["expires_at"] = time.time() + expires_in

    return data


def _get_headers() -> dict:
    """Build request headers with current valid token."""
    token = authenticate()
    return {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {token}",
    }


def get_user_id() -> str:
    """Get the authenticated user's ID."""
    authenticate()  # Ensure we're logged in
    return _token_store["user_id"]


# ══════════════════════════════════════════════
# Court Availability (NO AUTH REQUIRED)
# ══════════════════════════════════════════════

def get_court_availability(
    date: datetime,
    start_hour: int = 9,
    end_hour: int = 22,
) -> list:
    """
    Get available court slots for a given date.
    This endpoint is public (no authentication required).
    Playtomic limits queries to 25-hour windows.

    Args:
        date: The date to check.
        start_hour: Earliest hour to show (default 9).
        end_hour: Latest hour to show (default 22).

    Returns:
        Sorted list of slot dicts:
        [
            {
                "resource_id": "uuid",
                "resource_name": "Pista 1",
                "start_time": datetime (tz-aware),
                "duration_minutes": 90,
                "price": 24.00,
                "currency": "EUR",
            },
            ...
        ]

    Raises:
        PlaytomicAPIError: On network or API error.
    """
    tz = pytz.timezone(CALENDAR_TIMEZONE)
    start_min = tz.localize(datetime(date.year, date.month, date.day, start_hour, 0))
    start_max = tz.localize(datetime(date.year, date.month, date.day, end_hour, 0))

    # Enforce 25-hour max window
    if (start_max - start_min).total_seconds() > 25 * 3600:
        start_max = start_min + timedelta(hours=25)

    params = {
        "tenant_id": PLAYTOMIC_TENANT_ID,
        "sport_id": PLAYTOMIC_SPORT_ID,
        "local_start_min": start_min.strftime("%Y-%m-%dT%H:%M:%S"),
        "local_start_max": start_max.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    _rate_limit()

    try:
        resp = requests.get(
            f"{BASE_URL}/availability",
            params=params,
            headers=DEFAULT_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Playtomic availability check failed: {e}")
        raise PlaytomicAPIError(f"Failed to check court availability: {e}")

    # Parse response into clean slot dicts
    slots = []
    for resource in raw if isinstance(raw, list) else []:
        resource_id = resource.get("resource_id") or resource.get("id") or "unknown"
        resource_name = resource.get("name") or resource.get("resource_name") or _get_resource_name(resource_id)

        for slot in resource.get("slots", []):
            # Parse start_time — could be full ISO or just HH:MM:SS
            raw_time = slot.get("start_time", "")
            try:
                if "T" in raw_time:
                    slot_start = datetime.fromisoformat(raw_time)
                    if slot_start.tzinfo is None:
                        slot_start = tz.localize(slot_start)
                else:
                    # Time only: combine with the query date
                    parts = raw_time.split(":")
                    h, m = int(parts[0]), int(parts[1])
                    slot_start = tz.localize(
                        datetime(date.year, date.month, date.day, h, m)
                    )
            except (ValueError, IndexError):
                logger.warning(f"Could not parse slot time: {raw_time}")
                continue

            # Parse price — could be "12.00 EUR", float, or dict
            raw_price = slot.get("price", 0)
            if isinstance(raw_price, str):
                # "12.00 EUR" -> 12.00
                price = float(raw_price.split()[0]) if raw_price else 0
                currency = raw_price.split()[-1] if len(raw_price.split()) > 1 else "EUR"
            elif isinstance(raw_price, dict):
                price = float(raw_price.get("amount", 0))
                currency = raw_price.get("currency", "EUR")
            else:
                price = float(raw_price)
                currency = slot.get("currency", "EUR")

            slots.append({
                "resource_id": resource_id,
                "resource_name": resource_name,
                "start_time": slot_start,
                "duration_minutes": slot.get("duration", 90),
                "price": price,
                "currency": currency,
            })

    return sorted(slots, key=lambda s: s["start_time"])


def check_court_available(
    date: datetime,
    time_str: str,
    duration_minutes: int = 90,
) -> Optional[dict]:
    """
    Check if a specific court slot is available.
    Convenience wrapper around get_court_availability().

    Args:
        date: The date to check.
        time_str: "HH:MM" format (24h).
        duration_minutes: Desired slot duration (default 90 for padel).

    Returns:
        The matching slot dict if available, None if not.
    """
    target_hour, target_min = int(time_str.split(":")[0]), int(time_str.split(":")[1])

    slots = get_court_availability(date)

    for slot in slots:
        slot_h = slot["start_time"].hour
        slot_m = slot["start_time"].minute
        if slot_h == target_hour and slot_m == target_min and slot["duration_minutes"] >= duration_minutes:
            return slot

    # No exact match — try finding a slot within 30 minutes
    for slot in slots:
        slot_total = slot["start_time"].hour * 60 + slot["start_time"].minute
        target_total = target_hour * 60 + target_min
        if abs(slot_total - target_total) <= 30 and slot["duration_minutes"] >= duration_minutes:
            return slot

    return None


# ══════════════════════════════════════════════
# Classes / Lessons
# ══════════════════════════════════════════════

def get_upcoming_classes(
    from_date: datetime = None,
    status: str = "PENDING,IN_PROGRESS",
) -> list:
    """
    List upcoming group classes at the club.

    Args:
        from_date: Start date filter (default: now).
        status: Comma-separated status filter.

    Returns:
        List of class dicts:
        [
            {
                "class_id": "uuid",
                "name": "Padel Beginners",
                "start_time": datetime (tz-aware),
                "end_time": datetime (tz-aware),
                "instructor": "Coach Name",
                "spots_available": 3,
                "spots_total": 8,
                "price": 15.00,
                "currency": "EUR",
            },
            ...
        ]

    Raises:
        PlaytomicAPIError: On network or API error.
    """
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    if from_date is None:
        from_date = datetime.now(tz)
    elif from_date.tzinfo is None:
        from_date = tz.localize(from_date)

    params = {
        "tenant_id": PLAYTOMIC_TENANT_ID,  # singular — "tenant_ids" returns random clubs
        "sport_id": PLAYTOMIC_SPORT_ID,
        "from_start_date": from_date.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": status,
        "sort": "start_date,ASC",
        "size": 20,
        "page": 0,
    }

    _rate_limit()

    try:
        resp = requests.get(
            f"{BASE_URL}/classes",
            params=params,
            headers=_get_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Playtomic classes fetch failed: {e}")
        raise PlaytomicAPIError(f"Failed to fetch classes: {e}")

    classes = []
    items = raw if isinstance(raw, list) else raw.get("items", raw.get("content", []))

    for item in items:
        # Parse start/end times
        try:
            start_raw = item.get("start_date") or item.get("startDate", "")
            end_raw = item.get("end_date") or item.get("endDate", "")

            start_dt = datetime.fromisoformat(start_raw)
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)

            end_dt = datetime.fromisoformat(end_raw) if end_raw else start_dt + timedelta(hours=1)
            if end_dt.tzinfo is None:
                end_dt = tz.localize(end_dt)
        except (ValueError, TypeError):
            continue

        # Calculate duration in minutes from start/end
        duration_minutes = int((end_dt - start_dt).total_seconds() / 60)

        # Extract instructor name
        coaches = item.get("coaches", [])
        instructor = coaches[0].get("name", "TBD") if coaches else "TBD"

        # Determine class type (COURSE = group, PRIVATE = private lesson)
        class_type = item.get("type", "COURSE")

        # Extract registration/spots info
        reg = item.get("registration_info") or item.get("registrationInfo") or {}
        course = item.get("course_summary") or item.get("courseSummary") or {}
        max_players = course.get("max_players") or course.get("maxPlayers") or reg.get("max_players", 0)
        registered = reg.get("registered_players") or reg.get("registeredPlayers", 0)
        spots_available = max(0, max_players - registered) if max_players else 0

        # Extract price — API returns either a dict, a string like "10 EUR", or a number
        raw_price = reg.get("price") or item.get("price") or 0
        if isinstance(raw_price, dict):
            price = float(raw_price.get("amount", 0))
            currency = raw_price.get("currency", "EUR")
        elif isinstance(raw_price, str):
            # Parse strings like "10 EUR", "0 MXN", "85 EUR"
            parts = raw_price.strip().split()
            try:
                price = float(parts[0]) if parts else 0
            except ValueError:
                price = 0
            currency = parts[1] if len(parts) > 1 else "EUR"
        else:
            price = float(raw_price)
            currency = "EUR"

        classes.append({
            "class_id": item.get("academy_class_id") or item.get("id") or "unknown",
            "name": course.get("name") or item.get("name") or "Class",
            "class_type": class_type,  # "COURSE" or "PRIVATE"
            "start_time": start_dt,
            "end_time": end_dt,
            "duration_minutes": duration_minutes,
            "instructor": instructor,
            "spots_available": spots_available,
            "spots_total": max_players,
            "price": price,
            "currency": currency,
        })

    return classes


def get_upcoming_lessons(
    from_date: datetime = None,
) -> list:
    """
    List upcoming private lessons at the club.

    Args:
        from_date: Start date filter (default: now).

    Returns:
        List of lesson dicts (similar structure to classes).

    Raises:
        PlaytomicAPIError: On network or API error.
    """
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    if from_date is None:
        from_date = datetime.now(tz)
    elif from_date.tzinfo is None:
        from_date = tz.localize(from_date)

    params = {
        "tenant_id": PLAYTOMIC_TENANT_ID,
        "from_start_date": from_date.strftime("%Y-%m-%dT%H:%M:%S"),
        "sort": "start_date,ASC",
        "size": 20,
        "page": 0,
    }

    _rate_limit()

    try:
        resp = requests.get(
            f"{BASE_URL}/lessons",
            params=params,
            headers=_get_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Playtomic lessons fetch failed: {e}")
        raise PlaytomicAPIError(f"Failed to fetch lessons: {e}")

    lessons = []
    items = raw if isinstance(raw, list) else raw.get("items", raw.get("content", []))

    for item in items:
        try:
            start_raw = item.get("start_date") or item.get("startDate", "")
            end_raw = item.get("end_date") or item.get("endDate", "")

            start_dt = datetime.fromisoformat(start_raw)
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)

            end_dt = datetime.fromisoformat(end_raw) if end_raw else start_dt + timedelta(hours=1)
            if end_dt.tzinfo is None:
                end_dt = tz.localize(end_dt)
        except (ValueError, TypeError):
            continue

        # Calculate duration in minutes
        duration_minutes = int((end_dt - start_dt).total_seconds() / 60)

        raw_price = item.get("price", 0)
        if isinstance(raw_price, dict):
            price = float(raw_price.get("amount", 0))
            currency = raw_price.get("currency", "EUR")
        elif isinstance(raw_price, str):
            parts = raw_price.strip().split()
            try:
                price = float(parts[0]) if parts else 0
            except ValueError:
                price = 0
            currency = parts[1] if len(parts) > 1 else "EUR"
        else:
            price = float(raw_price)
            currency = "EUR"

        lessons.append({
            "lesson_id": item.get("tournament_id") or item.get("id") or "unknown",
            "name": item.get("tournament_name") or item.get("name") or "Lesson",
            "start_time": start_dt,
            "end_time": end_dt,
            "duration_minutes": duration_minutes,
            "instructor": "TBD",
            "spots_available": item.get("available_places") or item.get("availablePlaces") or 0,
            "spots_total": item.get("max_players") or item.get("maxPlayers") or 0,
            "price": price,
            "currency": currency,
        })

    return lessons


# ══════════════════════════════════════════════
# Booking (3-Step Payment Intent Flow)
# ══════════════════════════════════════════════

# Preferred payment methods in order of preference.
# CASH first — owner books on behalf of clients, no online payment.
_PAYMENT_METHOD_PREFERENCE = [
    "CASH", "MERCHANT_WALLET", "OFFER", "DIRECT",
    "QUICK_PAY", "CREDIT_CARD", "WALLET",
    "IDEAL", "BANCONTACT", "PAYTRAIL", "SWISH",
]


def create_booking(
    resource_id: str,
    start_time: datetime,
    duration_minutes: int = 90,
    payment_method_id: str = None,
) -> dict:
    """
    Book a court using the 3-step payment intent flow.

    Step 1: POST /payment_intents — create
    Step 2: PATCH /payment_intents/{id} — set payment method
    Step 3: POST /payment_intents/{id}/confirmation — confirm

    The payment method is auto-selected from the server's available
    methods using _PAYMENT_METHOD_PREFERENCE order (CASH first).

    Args:
        resource_id: The court/resource UUID.
        start_time: Booking start (timezone-aware datetime).
        duration_minutes: Slot duration (default 90 for padel).
        payment_method_id: Override payment method. None = auto-select.

    Returns:
        {
            "booking_id": "uuid (match_id)",
            "payment_intent_id": "uuid",
            "resource_name": "Pista 1",
            "start_time": datetime,
            "end_time": datetime,
            "price": 24.00,
            "currency": "EUR",
            "status": "CONFIRMED",
        }

    Raises:
        PlaytomicSlotTakenError: If the slot was grabbed by someone else.
        PlaytomicBookingError: On any step failure.
    """
    # Step 1: Create payment intent
    pi_id, available_methods = _create_payment_intent(resource_id, start_time, duration_minutes)

    # Step 2: Select best available payment method
    method = payment_method_id or _pick_best_payment_method(available_methods)
    _set_payment_method(pi_id, method)

    # Step 3: Confirm
    result = _confirm_payment_intent(pi_id)

    logger.info(f"Playtomic booking confirmed: {result.get('booking_id')}")
    return result


def _pick_best_payment_method(available_methods: list) -> str:
    """
    Choose the best payment method from what the server offers.
    Prefers CASH (no online charge) then falls back through preference list.

    Args:
        available_methods: List of dicts with 'payment_method_id' keys.

    Returns:
        The chosen payment_method_id string.

    Raises:
        PlaytomicBookingError: If no compatible method is available.
    """
    available_ids = {m.get("payment_method_id") for m in available_methods}

    for preferred in _PAYMENT_METHOD_PREFERENCE:
        if preferred in available_ids:
            logger.info(f"Selected payment method: {preferred}")
            return preferred

    # If none of our preferences match, use whatever's available
    if available_ids:
        fallback = next(iter(available_ids))
        logger.warning(f"No preferred payment method found; using fallback: {fallback}")
        return fallback

    raise PlaytomicBookingError(
        "No payment methods available for this club. "
        "Enable 'Cash' as an onsite payment method in Playtomic Manager > Settings.",
        step="set_payment",
    )


def _create_payment_intent(
    resource_id: str,
    start_time: datetime,
    duration_minutes: int,
) -> tuple:
    """
    Step 1: Create a payment intent for the court booking.

    The cart body uses nested structure: cart.requested_item.cart_item_data
    (discovered via reverse-engineered Go CLI and Laravel SDK).

    Returns:
        (payment_intent_id, available_payment_methods) tuple.

    Raises:
        PlaytomicSlotTakenError: If the slot is already taken (409).
        PlaytomicBookingError: On other failures.
    """
    tz = pytz.timezone(CALENDAR_TIMEZONE)
    if start_time.tzinfo is None:
        start_time = tz.localize(start_time)

    user_id = get_user_id()

    body = {
        "allowed_payment_method_types": [
            "OFFER", "CASH", "MERCHANT_WALLET", "DIRECT",
            "SWISH", "IDEAL", "BANCONTACT", "PAYTRAIL",
            "CREDIT_CARD", "QUICK_PAY", "WALLET",
        ],
        "user_id": user_id,
        "cart": {
            "requested_item": {
                "cart_item_type": "CUSTOMER_MATCH",
                "cart_item_voucher_id": None,
                "cart_item_data": {
                    "supports_split_payment": True,
                    "number_of_players": 4,
                    "tenant_id": PLAYTOMIC_TENANT_ID,
                    "resource_id": resource_id,
                    "start": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "duration": duration_minutes,
                    "match_registrations": [
                        {
                            "user_id": user_id,
                            "pay_now": False,  # Owner account — no real payment
                        }
                    ],
                },
            },
        },
    }

    _rate_limit()

    try:
        resp = requests.post(
            f"{BASE_URL}/payment_intents",
            json=body,
            headers=_get_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicBookingError(f"Network error creating payment intent: {e}", step="create_intent")

    if resp.status_code == 409:
        raise PlaytomicSlotTakenError("That court slot was just taken by someone else.")

    if resp.status_code not in (200, 201):
        raise PlaytomicBookingError(
            f"Failed to create payment intent: {resp.status_code} - {resp.text[:200]}",
            step="create_intent",
        )

    data = resp.json()
    pi_id = data.get("payment_intent_id") or data.get("id")
    if not pi_id:
        raise PlaytomicBookingError(
            f"No payment_intent_id in response: {list(data.keys())}",
            step="create_intent",
        )

    available_methods = data.get("available_payment_methods", [])
    logger.info(
        f"Payment intent created: {pi_id} | "
        f"Available methods: {[m.get('payment_method_id') for m in available_methods]}"
    )
    return pi_id, available_methods


def _set_payment_method(
    payment_intent_id: str,
    payment_method_id: str = None,
) -> bool:
    """
    Step 2: Attach a payment method to the payment intent.
    Uses PATCH with selected_payment_method_id field.

    The payment_method_id should come from _pick_best_payment_method()
    which auto-selects from the server's available_payment_methods list.

    Returns:
        True on success.

    Raises:
        PlaytomicBookingError: On failure.
    """
    if not payment_method_id:
        raise PlaytomicBookingError("No payment method specified.", step="set_payment")

    body = {
        "selected_payment_method_id": payment_method_id,
    }

    _rate_limit()

    try:
        resp = requests.patch(
            f"{BASE_URL}/payment_intents/{payment_intent_id}",
            json=body,
            headers=_get_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicBookingError(f"Network error setting payment method: {e}", step="set_payment")

    if resp.status_code not in (200, 204):
        raise PlaytomicBookingError(
            f"Failed to set payment method: {resp.status_code} - {resp.text[:200]}",
            step="set_payment",
        )

    logger.info(f"Payment method set for intent: {payment_intent_id}")
    return True


def _confirm_payment_intent(payment_intent_id: str) -> dict:
    """
    Step 3: Confirm the payment intent to finalize the booking.

    Returns:
        Booking details dict.

    Raises:
        PlaytomicSlotTakenError: If slot was grabbed during confirmation.
        PlaytomicBookingError: On other failures.
    """
    _rate_limit()

    try:
        resp = requests.post(
            f"{BASE_URL}/payment_intents/{payment_intent_id}/confirmation",
            json={},
            headers=_get_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicBookingError(f"Network error confirming booking: {e}", step="confirm")

    if resp.status_code == 409:
        raise PlaytomicSlotTakenError("Slot was taken during confirmation.")

    if resp.status_code not in (200, 201):
        raise PlaytomicBookingError(
            f"Booking confirmation failed: {resp.status_code} - {resp.text[:200]}",
            step="confirm",
        )

    data = resp.json()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    # The confirmation response may nest match data inside cart.item.cart_item_data
    # Try nested path first, then fall back to flat fields
    cart_item = {}
    cart = data.get("cart") or {}
    item = cart.get("item") or {}
    if item.get("cart_item_data"):
        cart_item = item["cart_item_data"]

    # Parse the result defensively — check nested path first, then flat
    start_raw = (
        cart_item.get("start_date") or cart_item.get("start") or
        data.get("start_date") or data.get("start") or ""
    )
    end_raw = (
        cart_item.get("end_date") or cart_item.get("end") or
        data.get("end_date") or data.get("end") or ""
    )

    try:
        start_dt = datetime.fromisoformat(start_raw)
        if start_dt.tzinfo is None:
            start_dt = tz.localize(start_dt)
    except (ValueError, TypeError):
        start_dt = None

    try:
        end_dt = datetime.fromisoformat(end_raw)
        if end_dt.tzinfo is None:
            end_dt = tz.localize(end_dt)
    except (ValueError, TypeError):
        end_dt = None

    # Extract match_id from nested or flat response
    booking_id = (
        cart_item.get("match_id") or
        data.get("match_id") or data.get("id") or payment_intent_id
    )

    return {
        "booking_id": booking_id,
        "payment_intent_id": payment_intent_id,
        "resource_name": _get_resource_name(
            cart_item.get("resource_id") or data.get("resource_id") or ""
        ),
        "start_time": start_dt,
        "end_time": end_dt,
        "price": float(cart_item.get("price") or data.get("price") or data.get("amount") or 0),
        "currency": data.get("currency") or cart_item.get("currency", "EUR"),
        "status": data.get("status", "CONFIRMED"),
    }


def enroll_in_class(class_id: str) -> dict:
    """
    Enroll the owner account in a group class.

    Args:
        class_id: The class UUID.

    Returns:
        {
            "enrollment_id": "uuid",
            "class_id": "uuid",
            "class_name": "Padel Beginners",
            "start_time": datetime or None,
            "status": "ENROLLED",
            "price": 15.00,
            "currency": "EUR",
        }

    Raises:
        PlaytomicBookingError: If class is full or enrollment fails.
    """
    _rate_limit()

    body = {
        "user_id": get_user_id(),
    }

    try:
        resp = requests.post(
            f"{BASE_URL}/classes/{class_id}/registrations",
            json=body,
            headers=_get_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicBookingError(f"Network error enrolling in class: {e}", step="enroll")

    if resp.status_code == 409:
        raise PlaytomicBookingError("This class is full or you're already enrolled.", step="enroll")

    if resp.status_code not in (200, 201):
        raise PlaytomicBookingError(
            f"Class enrollment failed: {resp.status_code} - {resp.text[:200]}",
            step="enroll",
        )

    data = resp.json()
    tz = pytz.timezone(CALENDAR_TIMEZONE)

    start_raw = data.get("start_date") or data.get("startDate") or ""
    try:
        start_dt = datetime.fromisoformat(start_raw)
        if start_dt.tzinfo is None:
            start_dt = tz.localize(start_dt)
    except (ValueError, TypeError):
        start_dt = None

    return {
        "enrollment_id": data.get("registration_id") or data.get("id") or class_id,
        "class_id": class_id,
        "class_name": data.get("class_name") or data.get("name") or "Class",
        "start_time": start_dt,
        "status": "ENROLLED",
        "price": float(data.get("price") or 0),
        "currency": data.get("currency", "EUR"),
    }


# ══════════════════════════════════════════════
# Cancel Booking
# ══════════════════════════════════════════════

def cancel_booking(match_id: str) -> bool:
    """
    Cancel a court booking or class enrollment.

    Args:
        match_id: The booking/match UUID from Playtomic.

    Returns:
        True if cancellation succeeded.

    Raises:
        PlaytomicAPIError: On failure (past cancellation window, etc.)
    """
    _rate_limit()

    try:
        resp = requests.delete(
            f"{BASE_URL}/matches/{match_id}",
            headers=_get_headers(),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        raise PlaytomicAPIError(f"Network error cancelling booking: {e}")

    # Some APIs use POST /matches/{id}/cancel instead of DELETE
    if resp.status_code == 405:
        try:
            resp = requests.post(
                f"{BASE_URL}/matches/{match_id}/cancel",
                json={"reason": "PERSONAL"},
                headers=_get_headers(),
                timeout=15,
            )
        except requests.exceptions.RequestException as e:
            raise PlaytomicAPIError(f"Network error cancelling booking (POST): {e}")

    if resp.status_code not in (200, 204):
        raise PlaytomicAPIError(
            f"Failed to cancel booking: {resp.status_code} - {resp.text[:200]}"
        )

    logger.info(f"Playtomic booking cancelled: {match_id}")
    return True


# ══════════════════════════════════════════════
# User Bookings
# ══════════════════════════════════════════════

def get_my_bookings(
    upcoming_only: bool = True,
    max_results: int = 20,
) -> list:
    """
    Get bookings for the authenticated user (owner account).

    Args:
        upcoming_only: If True, filter to future bookings.
        max_results: Max number of results.

    Returns:
        List of booking dicts:
        [
            {
                "match_id": "uuid",
                "resource_name": "Pista 1",
                "start_time": datetime (tz-aware),
                "end_time": datetime (tz-aware),
                "status": "CONFIRMED",
                "price": 24.00,
                "currency": "EUR",
            },
            ...
        ]

    Raises:
        PlaytomicAPIError: On network or API error.
    """
    user_id = get_user_id()

    params = {
        "user_id": user_id,
        "size": max_results,
        "sort": "start_date,ASC" if upcoming_only else "start_date,DESC",
    }

    _rate_limit()

    try:
        resp = requests.get(
            f"{BASE_URL}/matches",
            params=params,
            headers=_get_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Playtomic bookings fetch failed: {e}")
        raise PlaytomicAPIError(f"Failed to fetch bookings: {e}")

    tz = pytz.timezone(CALENDAR_TIMEZONE)
    now = datetime.now(tz)
    bookings = []

    items = raw if isinstance(raw, list) else raw.get("items", raw.get("content", raw.get("matches", [])))

    for item in items:
        try:
            start_raw = item.get("start_date") or item.get("startDate") or ""
            end_raw = item.get("end_date") or item.get("endDate") or ""

            start_dt = datetime.fromisoformat(start_raw)
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)

            end_dt = datetime.fromisoformat(end_raw) if end_raw else start_dt + timedelta(minutes=90)
            if end_dt.tzinfo is None:
                end_dt = tz.localize(end_dt)
        except (ValueError, TypeError):
            continue

        # Filter past bookings if requested
        if upcoming_only and start_dt < now:
            continue

        status = item.get("status") or item.get("game_status") or "CONFIRMED"

        bookings.append({
            "match_id": item.get("match_id") or item.get("id") or "unknown",
            "resource_name": _extract_resource_name(item),
            "start_time": start_dt,
            "end_time": end_dt,
            "status": status,
            "price": float(item.get("price") or item.get("amount") or 0),
            "currency": item.get("currency", "EUR"),
        })

    return bookings


def find_booking_by_date(date_str: str) -> list:
    """
    Find bookings on a specific date.
    Convenience wrapper: calls get_my_bookings() and filters.

    Args:
        date_str: "YYYY-MM-DD" format.

    Returns:
        List of matching booking dicts.
    """
    all_bookings = get_my_bookings(upcoming_only=False, max_results=50)
    return [
        b for b in all_bookings
        if b["start_time"].strftime("%Y-%m-%d") == date_str
    ]


def _extract_resource_name(item: dict) -> str:
    """Extract court/resource name from a match/booking item defensively."""
    # Try various known field paths
    resource = item.get("resource") or item.get("court") or {}
    if isinstance(resource, dict):
        return resource.get("name") or resource.get("resource_name") or "Court"

    # Try resource_properties
    props = item.get("resource_properties") or {}
    if isinstance(props, dict) and props.get("resource_type"):
        return f"{props.get('resource_type', 'Court')} {props.get('resource_size', '')}"

    # Tenant info as fallback
    tenant = item.get("tenant") or {}
    if isinstance(tenant, dict):
        return tenant.get("tenant_name") or "Court"

    return "Court"
