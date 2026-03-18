import os
import logging
from flask import Flask, request, jsonify
import anthropic
import requests
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GORGIAS_DOMAIN = os.environ.get("GORGIAS_DOMAIN")
GORGIAS_EMAIL = os.environ.get("GORGIAS_EMAIL")
GORGIAS_API_KEY = os.environ.get("GORGIAS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

MAX_MACROS = 20
MACRO_BODY_LIMIT = 200
MESSAGE_CHAR_LIMIT = 500

def gorgias_auth():
    return HTTPBasicAuth(GORGIAS_EMAIL, GORGIAS_API_KEY)

def fetch_ticket(ticket_id):
    url = f"https://{GORGIAS_DOMAIN}/api/tickets/{ticket_id}"
    resp = requests.get(url, auth=gorgias_auth(), timeout=10)
    resp.raise_for_status()
    return resp.json()

def fetch_macros():
    url = f"https://{GORGIAS_DOMAIN}/api/macros"
    resp = requests.get(url, auth=gorgias_auth(), params={"limit": MAX_MACROS}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    macros = data.get("data", [])
    result = []
    for m in macros[:MAX_MACROS]:
        body = ""
        for action in m.get("actions", []):
            if action.get("type") == "addMessage":
                body = action.get("body", "")[:MACRO_BODY_LIMIT]
                break
        result.append({"id": m["id"], "name": m.get("name", ""), "body": body})
    return result

def extract_customer_message(ticket):
    messages = ticket.get("messages", [])
    for msg in messages:
        if msg.get("channel") == "internal-note":
            continue
        body = msg.get("body_text") or msg.get("body", "")
        if body:
            return body[:MESSAGE_CHAR_LIMIT]
    subject = ticket.get("subject", "")
    return subject[:MESSAGE_CHAR_LIMIT] if subject else ""

def build_prompt(ticket, macros, customer_message):
    macro_lines = "\n".join(
        f"- [{m['id']}] {m['name']}: {m['body']}" for m in macros
    ) or "No macros available."
    return f"""You are a customer support assistant. Analyze the following support ticket and respond in exactly this format:

SUMMARY: <one sentence describing the customer's issue>
CATEGORY: <one of: billing / shipping / product / returns / other>
URGENCY: <one of: low / medium / high>
BEST_MACRO: <macro name from the list below, or "none">
DRAFT_RESPONSE: <a short, friendly draft reply to the customer>

Ticket subject: {ticket.get('subject', 'N/A')}
Customer message:
{customer_message}

Available macros:
{macro_lines}

Respond only in the format above. Do not add extra commentary."""

def parse_claude_response(text):
    lines = {}
    for line in text.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines

def format_internal_note(parsed):
    summary = parsed.get("SUMMARY", "N/A")
    category = parsed.get("CATEGORY", "N/A")
    urgency = parsed.get("URGENCY", "N/A")
    macro = parsed.get("BEST_MACRO", "none")
    draft = parsed.get("DRAFT_RESPONSE", "N/A")
    return (
        f"🤖 Claude Analysis\n\n"
        f"Summary: {summary}\n"
        f"Category: {category}\n"
        f"Urgency: {urgency}\n"
        f"Best Macro: {macro}\n\n"
        f"Draft Response:\n{draft}"
    )

def post_internal_note(ticket_id, body):
    url = f"https://{GORGIAS_DOMAIN}/api/tickets/{ticket_id}/messages"
    payload = {
        "channel": "internal-note",
        "body_text": body,
    }
    resp = requests.post(url, json=payload, auth=gorgias_auth(), timeout=10)
    resp.raise_for_status()
    return resp.json()

def analyze_ticket(ticket_id):
    ticket = fetch_ticket(ticket_id)
    customer_message = extract_customer_message(ticket)
    if not customer_message:
        logger.info("Ticket %s has no customer message yet, skipping.", ticket_id)
        return
    macros = fetch_macros()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(ticket, macros, customer_message)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = message.content[0].text
    parsed = parse_claude_response(raw_text)
    note_body = format_internal_note(parsed)
    post_internal_note(ticket_id, note_body)
    logger.info("Posted Claude analysis note to ticket %s.", ticket_id)

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "gorgias-claude-webhook"}), 200

@app.route("/webhook/gorgias", methods=["POST"])
def gorgias_webhook():
    try:
        payload = request.get_json(silent=True) or {}

        # Handle Gorgias sending just a ticket_id directly
        ticket_id = (
            payload.get("ticket_id") or
            (payload.get("data", {}).get("ticket") or {}).get("id") or
            (payload.get("ticket") or {}).get("id")
        )

        if not ticket_id:
            logger.info("No ticket ID found, ignoring.")
            return jsonify({"status": "ignored"}), 200

        # Skip if the triggering message is already an internal note (avoid loops)
        message_data = payload.get("data", {}).get("message") or payload.get("message", {})
        if message_data.get("channel") == "internal-note":
            logger.info("Skipping internal-note message on ticket %s.", ticket_id)
            return jsonify({"status": "skipped internal note"}), 200

        analyze_ticket(ticket_id)

    except Exception as e:
        logger.error("Error processing webhook: %s", e, exc_info=True)

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)