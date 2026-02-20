# WhatsApp Class Scheduling Agent

## Purpose
A WhatsApp bot that manages class bookings and padel court reservations via natural language. Students and the owner can schedule, reschedule, cancel classes, book courts, enroll in group classes, and check availability — all through WhatsApp messages. Events are synced to Google Calendar (private classes) and Playtomic (courts and club classes).

## Stack
- **WhatsApp**: Cloud API (Meta) — webhook-based
- **NLP**: Claude API (Anthropic) — parses natural language into structured intents
- **Calendar**: Google Calendar API — private class event CRUD and availability
- **Courts/Club**: Playtomic API (reverse-engineered consumer API) — court availability, booking, class enrollment, cancellation
- **State**: SQLite — conversation history, pending operations, event/booking mapping
- **Server**: Flask + Gunicorn on Railway/Render

## Scripts (execution/)

| Script | Role |
|--------|------|
| `webhook_server.py` | Flask app. GET /webhook (verify), POST /webhook (messages), GET /health |
| `whatsapp_api.py` | Send messages, templates, mark as read, parse payloads |
| `google_calendar.py` | Auth (OAuth2 + auto-refresh), create/update/delete events, free/busy |
| `playtomic_api.py` | Playtomic auth (email/pw + JWT refresh), court availability, 3-step booking, class enrollment, cancel |
| `intent_parser.py` | Claude API -> structured JSON (intent, entities incl. service/sub_type, confidence) |
| `conversation_handler.py` | Core orchestrator — routes intents to Calendar or Playtomic actions, formats replies |
| `conversation_state.py` | SQLite DB for history, pending ops, event mapping, Playtomic booking tracking |
| `setup_google_auth.py` | One-time local script to generate token.json |

## Workflows

### Schedule a Class (Google Calendar)
1. User: "Book a class tomorrow at 3pm"
2. `intent_parser` -> intent=schedule, service=calendar, date=YYYY-MM-DD, time=15:00
3. `google_calendar.check_availability()` -> is slot free?
4. If free -> set pending operation, ask confirmation
5. User: "yes" -> `create_event()`, `save_scheduled_event()`, send confirmation
6. If taken -> `list_available_slots()` -> suggest 3 alternatives

### Reschedule (Google Calendar)
1. User: "Move my Monday class to Tuesday 4pm"
2. Find existing event via `find_user_event_by_date()`
3. Check new slot availability
4. Pending operation -> confirm -> `update_event()`

### Cancel (Google Calendar)
1. User: "Cancel my Friday class"
2. Find event -> ask confirmation -> `delete_event()`, `delete_scheduled_event()`

### Check Availability (Google Calendar)
1. User: "What's available next Monday?"
2. `list_available_slots(date)` -> format and send slot list

### Check Schedule
1. Owner: sees all calendar events (next 7 days) + Playtomic bookings
2. Student: sees only their own booked classes + Playtomic bookings
3. When service=null, handler shows combined view from both systems

### Book a Padel Court (Playtomic)
1. User: "Book a court tomorrow at 5pm"
2. `intent_parser` -> intent=book_court (or schedule with service=playtomic), sub_type=court, duration=90
3. `playtomic_api.check_court_available()` -> find matching slot
4. If available -> show court name + price, set pending operation (type=playtomic_book_court)
5. User: "yes" -> `create_booking()` (3-step payment intent), `save_playtomic_booking()`, send confirmation
6. If taken -> `get_court_availability()` -> suggest alternatives with prices

### Enroll in Club Class (Playtomic)
1. User: "Show me group classes" or "Sign me up for beginners class"
2. `playtomic_api.get_upcoming_classes()` -> list available classes with spots/prices
3. User picks one -> pending operation (type=playtomic_enroll_class)
4. User: "yes" -> `enroll_in_class()`, `save_playtomic_booking(type=class)`

### Cancel Playtomic Booking
1. User: "Cancel my court booking"
2. Check `playtomic_bookings` table for user's active bookings
3. Pending operation (type=playtomic_cancel) -> confirm -> `cancel_booking()`, soft-delete in DB

### Playtomic Reschedule
1. Playtomic doesn't support direct reschedule
2. Bot guides user: cancel existing + book new time
3. Cancellation policies may apply

### Service Disambiguation
1. User: "Cancel my booking" (ambiguous — no service specified)
2. Check both `scheduled_events` (Calendar) and `playtomic_bookings` (Playtomic)
3. If only one system has bookings -> route there automatically
4. If both -> ask: "Are you cancelling a private class or a court booking?"

## Edge Cases

### Ambiguous Messages
- "I want a class" (no date/time) -> Claude sets `clarification_needed=true`, bot asks follow-up
- Partial data stored in conversation state, combined with next message

### Service Ambiguity
- When NLP can't determine service (calendar vs playtomic), confidence is lower
- Handler checks both systems and auto-routes if only one has data
- If both have data, asks user to clarify
- Keywords: "court/pista/cancha" -> playtomic; "private class/lesson/profesor" -> calendar

### Slot Conflicts (Calendar)
- `check_availability()` returns False -> call `list_available_slots()` -> show 3 alternatives
- Concurrent requests: Calendar API is source of truth, second request will see slot as taken

### Slot Race Conditions (Playtomic)
- Between availability check and booking confirmation, another user may take the slot
- `PlaytomicSlotTakenError` caught specifically -> friendly "just taken" message with alternatives

### Playtomic Payment Failures
- 3-step booking flow can fail at any step:
  - Step 1 fail (create intent): slot may already be taken -> suggest alternatives
  - Step 2 fail (payment method): account payment issue -> tell user to check Playtomic app
  - Step 3 fail (confirm): race condition, slot grabbed -> suggest alternatives
- Payment intent auto-expires; no manual cleanup needed

### Playtomic Cancellation Window
- Playtomic may enforce cancellation deadlines (e.g., 24h before)
- API will return error -> catch and inform user about policy

### Pending Operation Conflicts
- User sends new request while pending exists -> bot asks to confirm/cancel current first

### Token Expiry (Google Calendar)
- `authenticate_calendar()` auto-refreshes via refresh_token
- If refresh fails -> log error, notify owner via WhatsApp

### Token Expiry (Playtomic)
- `authenticate()` auto-refreshes JWT tokens via refresh_token
- If refresh fails -> falls back to full email/password login
- If login fails -> log error, tell user "Playtomic is temporarily unavailable"

### Playtomic API Changes (Reverse-Engineered)
- API is unofficial; endpoints may change without notice
- All API calls wrapped in try/except with graceful degradation
- Response parsing is defensive (use .get() everywhere, handle missing fields)
- If systematic failures detected -> log pattern, notify owner

### 24-Hour Window (WhatsApp Policy)
- Track `last_message_time` per user
- Outside 24h -> use `send_template_message()` with pre-approved Meta template
- Template must be submitted and approved via Meta Business Manager

### Owner vs Student
- `OWNER_PHONE` in .env identifies admin
- Owner gets: admin view of full calendar + all Playtomic bookings
- Students: can only see/manage their own events and bookings

### Timezone
- Single timezone from `CALENDAR_TIMEZONE` in .env
- All confirmations include explicit timezone ("3:00 PM EST")
- Claude resolves relative dates ("tomorrow") using current date in that timezone

## Environment Variables
```
# WhatsApp
WHATSAPP_TOKEN          - Meta access token
PHONE_NUMBER_ID         - WhatsApp phone number ID
VERIFY_TOKEN            - Webhook verification secret
WHATSAPP_API_VERSION    - Graph API version (v21.0)

# Claude
ANTHROPIC_API_KEY       - Claude API key

# Google Calendar
CALENDAR_TIMEZONE       - e.g., America/New_York
CALENDAR_ID             - Google Calendar ID (default: primary)

# Playtomic
PLAYTOMIC_EMAIL         - Owner's Playtomic login email
PLAYTOMIC_PASSWORD      - Owner's Playtomic login password
PLAYTOMIC_TENANT_ID     - Club UUID (from Playtomic URL or browser network tab)
PLAYTOMIC_SPORT_ID      - Primary sport (default: PADEL)

# General
OWNER_PHONE             - Owner's WhatsApp number
PORT                    - Server port (default: 5000)
```

## Setup Checklist

### Prerequisites
- [ ] Meta Developer Account created
- [ ] WhatsApp Cloud API app configured
- [ ] Google Cloud project with Calendar API enabled
- [ ] OAuth2 Desktop credentials downloaded as `credentials.json`
- [ ] Anthropic API key
- [ ] Playtomic account (email/password)
- [ ] Playtomic tenant_id for your club (find via browser network tab on club page)

### Local Development
- [ ] `pip install -r requirements.txt`
- [ ] Fill in `.env` with all API keys (including Playtomic credentials)
- [ ] Run `python execution/setup_google_auth.py` (generates token.json)
- [ ] Install ngrok: `npm install -g ngrok` or download
- [ ] Start server: `python execution/webhook_server.py`
- [ ] Start ngrok: `ngrok http 5000`
- [ ] Set webhook URL in Meta Developer Console to ngrok URL + `/webhook`
- [ ] Test: Send "hi" from WhatsApp
- [ ] Test: "What courts are available tomorrow?" (Playtomic availability — no auth needed)
- [ ] Test: "Book a court tomorrow at 5pm" (full booking flow — requires valid Playtomic account)

### Deployment (Railway/Render)
- [ ] Push code to GitHub
- [ ] Create project on Railway/Render
- [ ] Add all env vars from `.env` (including PLAYTOMIC_* vars)
- [ ] Upload token.json as secret/file
- [ ] Deploy and get production URL
- [ ] Update webhook URL in Meta Developer Console
- [ ] Submit message template for 24h+ window (Meta Business Manager)

## Error Handling
- WhatsApp API failure -> retry 3x, log error
- Claude API failure -> fallback to keyword-based `_fallback_parse()`
- Calendar API failure -> tell user "calendar temporarily unavailable"
- Playtomic API failure -> tell user "Playtomic temporarily unavailable", log error
- Playtomic booking failure -> catch step-specific errors, show friendly messages
- SQLite locked -> WAL mode handles concurrent access
- Webhook always returns 200 to prevent Meta retries

## Monitoring
- Logs: `.tmp/bot_operations.log`
- Health check: GET `/health`
- All Calendar operations logged with event IDs
- All Playtomic operations logged with match IDs
- All WhatsApp messages logged (sent/received)

## Learnings
- Playtomic API is reverse-engineered from the consumer web app; endpoints may change.
  If systematic 404s or format changes appear, inspect the web app's network traffic to find updated endpoints.
- Court availability endpoint requires NO authentication (public data).
- Booking requires a 3-step payment intent flow — partial failures must be handled gracefully.
- Max 25-hour query window for availability — split multi-day queries if needed.
- Default padel match duration is 90 minutes, not 60.
- Rate limiting: 0.5s minimum between consecutive API calls to avoid undocumented rate limits.
- Auth endpoint: `https://api.playtomic.io/v3/auth/login` — uses email/password, returns JWT.
- Response field names vary between camelCase and snake_case — always check both patterns.
