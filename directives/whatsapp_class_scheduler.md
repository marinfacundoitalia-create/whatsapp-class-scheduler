# WhatsApp Class Scheduling Agent

## Purpose
A WhatsApp bot that manages class bookings via natural language. Students and the owner can schedule, reschedule, cancel classes, and check availability — all through WhatsApp messages. Events are synced to Google Calendar.

## Stack
- **WhatsApp**: Cloud API (Meta) — webhook-based
- **NLP**: Claude API (Anthropic) — parses natural language into structured intents
- **Calendar**: Google Calendar API — event CRUD and availability
- **State**: SQLite — conversation history, pending operations, event mapping
- **Server**: Flask + Gunicorn on Railway/Render

## Scripts (execution/)

| Script | Role |
|--------|------|
| `webhook_server.py` | Flask app. GET /webhook (verify), POST /webhook (messages), GET /health |
| `whatsapp_api.py` | Send messages, templates, mark as read, parse payloads |
| `google_calendar.py` | Auth (OAuth2 + auto-refresh), create/update/delete events, free/busy |
| `intent_parser.py` | Claude API → structured JSON (intent, entities, confidence) |
| `conversation_handler.py` | Core orchestrator — routes intents → calendar actions → replies |
| `conversation_state.py` | SQLite DB for history, pending ops, event-to-user mapping |
| `setup_google_auth.py` | One-time local script to generate token.json |

## Workflows

### Schedule a Class
1. User: "Book a class tomorrow at 3pm"
2. `intent_parser` → intent=schedule, date=YYYY-MM-DD, time=15:00
3. `google_calendar.check_availability()` → is slot free?
4. If free → set pending operation, ask confirmation
5. User: "yes" → `create_event()`, `save_scheduled_event()`, send confirmation
6. If taken → `list_available_slots()` → suggest 3 alternatives

### Reschedule
1. User: "Move my Monday class to Tuesday 4pm"
2. Find existing event via `find_user_event_by_date()`
3. Check new slot availability
4. Pending operation → confirm → `update_event()`

### Cancel
1. User: "Cancel my Friday class"
2. Find event → ask confirmation → `delete_event()`, `delete_scheduled_event()`

### Check Availability
1. User: "What's available next Monday?"
2. `list_available_slots(date)` → format and send slot list

### Check Schedule
1. Owner: sees all calendar events (next 7 days)
2. Student: sees only their own booked classes

## Edge Cases

### Ambiguous Messages
- "I want a class" (no date/time) → Claude sets `clarification_needed=true`, bot asks follow-up
- Partial data stored in conversation state, combined with next message

### Slot Conflicts
- `check_availability()` returns False → call `list_available_slots()` → show 3 alternatives
- Concurrent requests: Calendar API is source of truth, second request will see slot as taken

### Pending Operation Conflicts
- User sends new request while pending exists → bot asks to confirm/cancel current first

### Token Expiry (Google Calendar)
- `authenticate_calendar()` auto-refreshes via refresh_token
- If refresh fails → log error, notify owner via WhatsApp

### 24-Hour Window (WhatsApp Policy)
- Track `last_message_time` per user
- Outside 24h → use `send_template_message()` with pre-approved Meta template
- Template must be submitted and approved via Meta Business Manager

### Owner vs Student
- `OWNER_PHONE` in .env identifies admin
- Owner gets: "Show all bookings today", admin view of full calendar
- Students: can only see/manage their own events

### Timezone
- Single timezone from `CALENDAR_TIMEZONE` in .env
- All confirmations include explicit timezone ("3:00 PM EST")
- Claude resolves relative dates ("tomorrow") using current date in that timezone

## Environment Variables
```
WHATSAPP_TOKEN          - Meta access token
PHONE_NUMBER_ID         - WhatsApp phone number ID
VERIFY_TOKEN            - Webhook verification secret
WHATSAPP_API_VERSION    - Graph API version (v21.0)
ANTHROPIC_API_KEY       - Claude API key
CALENDAR_TIMEZONE       - e.g., America/New_York
CALENDAR_ID             - Google Calendar ID (default: primary)
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

### Local Development
- [ ] `pip install -r requirements.txt`
- [ ] Fill in `.env` with all API keys
- [ ] Run `python execution/setup_google_auth.py` (generates token.json)
- [ ] Install ngrok: `npm install -g ngrok` or download
- [ ] Start server: `python execution/webhook_server.py`
- [ ] Start ngrok: `ngrok http 5000`
- [ ] Set webhook URL in Meta Developer Console to ngrok URL + `/webhook`
- [ ] Send a test message from WhatsApp

### Deployment (Railway/Render)
- [ ] Push code to GitHub
- [ ] Create project on Railway/Render
- [ ] Add all env vars from `.env`
- [ ] Upload token.json as secret/file
- [ ] Deploy and get production URL
- [ ] Update webhook URL in Meta Developer Console
- [ ] Submit message template for 24h+ window (Meta Business Manager)

## Error Handling
- WhatsApp API failure → retry 3x, log error
- Claude API failure → fallback to keyword-based `_fallback_parse()`
- Calendar API failure → tell user "calendar temporarily unavailable"
- SQLite locked → WAL mode handles concurrent access
- Webhook always returns 200 to prevent Meta retries

## Monitoring
- Logs: `.tmp/bot_operations.log`
- Health check: GET `/health`
- All Calendar operations logged with event IDs
- All WhatsApp messages logged (sent/received)

## Learnings
<!-- Update this section as you discover issues, API limits, edge cases -->
- _No learnings yet — system is new_
