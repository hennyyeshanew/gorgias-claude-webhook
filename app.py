import os
import logging
import json
from flask import Flask, request, jsonify, Response
from datetime import datetime
import anthropic
import requests
from requests.auth import HTTPBasicAuth
import secrets

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GORGIAS_DOMAIN = os.environ.get("GORGIAS_DOMAIN")
GORGIAS_EMAIL = os.environ.get("GORGIAS_EMAIL")
GORGIAS_API_KEY = os.environ.get("GORGIAS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

MAX_MACROS = 20
MACRO_BODY_LIMIT = 200
MESSAGE_CHAR_LIMIT = 500
REPORT_TICKET_LIMIT = 500

def gorgias_auth():
    return HTTPBasicAuth(GORGIAS_EMAIL, GORGIAS_API_KEY)

def fetch_ticket(ticket_id):
    url = f"https://broyaliving.gorgias.com/api/tickets/{ticket_id}"
    resp = requests.get(url, auth=gorgias_auth(), timeout=10)
    resp.raise_for_status()
    return resp.json()

def fetch_macros():
    url = f"https://broyaliving.gorgias.com/api/macros"
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

def fetch_tickets_for_report(limit=500):
    url = f"https://broyaliving.gorgias.com/api/tickets"
    all_tickets = []
    cursor = None

    while len(all_tickets) < limit:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, auth=gorgias_auth(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        tickets = data.get("data", [])
        if not tickets:
            break
        all_tickets.extend(tickets)

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break

    return all_tickets[:limit]

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
    url = f"https://broyaliving.gorgias.com/api/tickets/{ticket_id}/messages"
    payload = {
        "channel": "internal-note",
        "body_text": body,
        "body_html": body.replace("\n", "<br>"),
        "via": "internal-note",
        "source": {
            "type": "internal-note",
            "from": {"address": GORGIAS_EMAIL}
        }
    }
    resp = requests.post(url, json=payload, auth=gorgias_auth(), timeout=10)
    resp.raise_for_status()
    return resp.json()

def analyze_ticket(ticket_id):
    ticket = fetch_ticket(ticket_id)
    for msg in ticket.get("messages", []):
        if msg.get("channel") == "internal-note":
            body = msg.get("body_text", "")
            if "Claude Analysis" in body:
                logger.info("Ticket %s already has Claude note, skipping.", ticket_id)
                return
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

def compute_report_stats(tickets):
    categories = {}
    agents = {}
    hours = {}
    days = {}
    response_times = []
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for ticket in tickets:
        subject = (ticket.get("subject") or "").lower()
        if any(w in subject for w in ["ship", "delivery", "track", "order"]):
            cat = "Shipping"
        elif any(w in subject for w in ["refund", "return", "exchange"]):
            cat = "Returns"
        elif any(w in subject for w in ["bill", "charge", "payment", "invoice"]):
            cat = "Billing"
        elif any(w in subject for w in ["product", "broken", "defect", "quality"]):
            cat = "Product Issue"
        else:
            cat = "Other"
        categories[cat] = categories.get(cat, 0) + 1

        assignee = ticket.get("assignee_user")
        if assignee:
            name = f"{assignee.get('firstname', '')} {assignee.get('lastname', '')}".strip()
            if name:
                if name not in agents:
                    agents[name] = {"assigned": 0, "closed": 0}
                agents[name]["assigned"] += 1
                if ticket.get("status") == "closed":
                    agents[name]["closed"] += 1

        created = ticket.get("created_datetime")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                hour_label = f"{dt.hour:02d}:00"
                hours[hour_label] = hours.get(hour_label, 0) + 1
                day_label = day_names[dt.weekday()]
                days[day_label] = days.get(day_label, 0) + 1
            except Exception:
                pass

        messages = ticket.get("messages", [])
        customer_time = None
        agent_time = None
        for msg in messages:
            if msg.get("channel") == "internal-note":
                continue
            sender = msg.get("sender", {})
            created_msg = msg.get("created_datetime")
            if not created_msg:
                continue
            try:
                msg_dt = datetime.fromisoformat(created_msg.replace("Z", "+00:00"))
                if sender.get("type") == "customer" and customer_time is None:
                    customer_time = msg_dt
                elif sender.get("type") == "agent" and customer_time and agent_time is None:
                    agent_time = msg_dt
                    diff = (agent_time - customer_time).total_seconds() / 60
                    if 0 < diff < 10000:
                        response_times.append(diff)
                    break
            except Exception:
                pass

    avg_response = round(sum(response_times) / len(response_times), 1) if response_times else None
    top_hour = max(hours, key=hours.get) if hours else "N/A"
    top_day = max(days, key=days.get) if days else "N/A"
    sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    sorted_agents = sorted(agents.items(), key=lambda x: x[1]["assigned"], reverse=True)

    return {
        "total_tickets": len(tickets),
        "categories": sorted_categories,
        "agents": sorted_agents,
        "avg_response_minutes": avg_response,
        "busiest_hour": top_hour,
        "busiest_day": top_day,
        "hours": hours,
        "days": days,
    }

def generate_claude_insights(stats, tickets):
    sample = [{"subject": t.get("subject", ""), "status": t.get("status", "")} for t in tickets[:50]]
    prompt = f"""You are a customer support analytics expert. Based on the following support ticket statistics, provide a concise executive summary with key insights and actionable recommendations.

STATS:
- Total tickets analyzed: {stats['total_tickets']}
- Average first response time: {stats['avg_response_minutes']} minutes
- Busiest day: {stats['busiest_day']}
- Busiest hour: {stats['busiest_hour']}
- Top categories: {stats['categories'][:5]}
- Agent workload: {stats['agents'][:5]}

SAMPLE TICKET SUBJECTS:
{json.dumps([t['subject'] for t in sample], indent=2)}

Please provide:
1. A 2-3 sentence executive summary
2. Top 3 trends you notice
3. Top 3 actionable recommendations for the CS team
4. Any concerning patterns to watch

Keep it concise and practical."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

def send_slack_report(stats, insights):
    if not SLACK_WEBHOOK_URL:
        logger.info("No Slack webhook configured, skipping.")
        return

    avg_resp = f"{stats['avg_response_minutes']} min" if stats['avg_response_minutes'] else "N/A"
    top_categories = "\n".join(
        f"• {cat}: {count} tickets" for cat, count in stats["categories"][:5]
    )
    top_agents = "\n".join(
        f"• {name}: {data['assigned']} assigned, {data['closed']} closed"
        for name, data in stats["agents"][:5]
    ) or "No agent data available"
    short_insights = insights[:800] + "..." if len(insights) > 800 else insights

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🤖 Broya Living CS Report"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tickets Analyzed:*\n{stats['total_tickets']}"},
                    {"type": "mrkdwn", "text": f"*Avg Response Time:*\n{avg_resp}"},
                    {"type": "mrkdwn", "text": f"*Busiest Day:*\n{stats['busiest_day']}"},
                    {"type": "mrkdwn", "text": f"*Busiest Hour:*\n{stats['busiest_hour']}"}
                ]
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📂 Top Categories:*\n{top_categories}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*👥 Agent Performance:*\n{top_agents}"}
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*🧠 Claude's Insights:*\n{short_insights}"}
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')}"}
                ]
            }
        ]
    }

    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    logger.info("Slack report sent, status: %s", resp.status_code)

def render_report_html(stats, insights):
    categories_rows = "".join(
        f"<tr><td>{cat}</td><td>{count}</td><td>{round(count/stats['total_tickets']*100)}%</td></tr>"
        for cat, count in stats["categories"]
    )
    agents_rows = "".join(
        f"<tr><td>{name}</td><td>{data['assigned']}</td><td>{data['closed']}</td><td>{round(data['closed']/data['assigned']*100) if data['assigned'] else 0}%</td></tr>"
        for name, data in stats["agents"]
    ) or "<tr><td colspan='4'>No agent data available</td></tr>"

    hours_bars = ""
    if stats["hours"]:
        max_h = max(stats["hours"].values())
        for h in sorted(stats["hours"].keys()):
            pct = round(stats["hours"][h] / max_h * 100)
            hours_bars += f'<div class="bar-row"><span class="bar-label">{h}</span><div class="bar" style="width:{pct}%">{stats["hours"][h]}</div></div>'

    days_bars = ""
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if stats["days"]:
        max_d = max(stats["days"].values())
        for d in day_order:
            if d in stats["days"]:
                pct = round(stats["days"][d] / max_d * 100)
                days_bars += f'<div class="bar-row"><span class="bar-label">{d}</span><div class="bar" style="width:{pct}%">{stats["days"][d]}</div></div>'

    insights_html = insights.replace("\n", "<br>")
    avg_resp = f"{stats['avg_response_minutes']} min" if stats['avg_response_minutes'] else "N/A"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CS Report — Broya Living</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
  .header {{ background: #1a1a2e; color: white; padding: 30px 40px; }}
  .header h1 {{ font-size: 24px; font-weight: 600; }}
  .header p {{ opacity: 0.7; margin-top: 4px; font-size: 14px; }}
  .container {{ max-width: 1100px; margin: 30px auto; padding: 0 20px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
  .stat-card {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  .stat-card .value {{ font-size: 32px; font-weight: 700; color: #1a1a2e; }}
  .stat-card .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
  .card {{ background: white; border-radius: 10px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 24px; }}
  .card h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 16px; color: #1a1a2e; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 8px 12px; background: #f8f8f8; color: #666; font-weight: 500; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f0f0f0; }}
  tr:last-child td {{ border-bottom: none; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  .bar-row {{ display: flex; align-items: center; margin-bottom: 8px; font-size: 13px; }}
  .bar-label {{ width: 90px; color: #666; flex-shrink: 0; }}
  .bar {{ background: #4f46e5; color: white; font-size: 11px; padding: 4px 8px; border-radius: 4px; min-width: 30px; }}
  .insights {{ background: #f0f4ff; border-left: 4px solid #4f46e5; padding: 20px; border-radius: 0 8px 8px 0; font-size: 14px; line-height: 1.7; }}
  .generated {{ text-align: center; color: #aaa; font-size: 12px; margin: 20px 0; }}
</style>
</head>
<body>
<div class="header">
  <h1>🤖 CS Team Report — Broya Living</h1>
  <p>Generated {datetime.now().strftime("%B %d, %Y at %I:%M %p")} · Last {stats['total_tickets']} tickets analyzed</p>
</div>
<div class="container">
  <div class="stats-grid">
    <div class="stat-card"><div class="value">{stats['total_tickets']}</div><div class="label">Tickets Analyzed</div></div>
    <div class="stat-card"><div class="value">{avg_resp}</div><div class="label">Avg First Response</div></div>
    <div class="stat-card"><div class="value">{stats['busiest_day']}</div><div class="label">Busiest Day</div></div>
    <div class="stat-card"><div class="value">{stats['busiest_hour']}</div><div class="label">Busiest Hour</div></div>
  </div>

  <div class="card">
    <h2>🧠 Claude's Insights & Recommendations</h2>
    <div class="insights">{insights_html}</div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>📂 Ticket Categories</h2>
      <table>
        <tr><th>Category</th><th>Count</th><th>Share</th></tr>
        {categories_rows}
      </table>
    </div>
    <div class="card">
      <h2>👥 Agent Performance</h2>
      <table>
        <tr><th>Agent</th><th>Assigned</th><th>Closed</th><th>Close Rate</th></tr>
        {agents_rows}
      </table>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>📅 Busiest Days</h2>
      {days_bars or "<p style='color:#aaa;font-size:13px'>No data available</p>"}
    </div>
    <div class="card">
      <h2>🕐 Busiest Hours</h2>
      {hours_bars or "<p style='color:#aaa;font-size:13px'>No data available</p>"}
    </div>
  </div>

  <p class="generated">Report generated by Claude · Broya Living CS Dashboard</p>
</div>
</body>
</html>"""

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "gorgias-claude-webhook"}), 200

@app.route("/report", methods=["GET"])
def report():
    try:
        logger.info("Generating CS report...")
        tickets = fetch_tickets_for_report(limit=REPORT_TICKET_LIMIT)
        stats = compute_report_stats(tickets)
        insights = generate_claude_insights(stats, tickets)
        send_slack_report(stats, insights)
        html = render_report_html(stats, insights)
        return Response(html, mimetype="text/html")
    except Exception as e:
        logger.error("Error generating report: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/webhook/gorgias", methods=["POST"])
def gorgias_webhook():
    try:
        payload = request.get_json(silent=True) or {}

        ticket_id = (
            payload.get("ticket_id") or
            (payload.get("data", {}).get("ticket") or {}).get("id") or
            (payload.get("ticket") or {}).get("id")
        )

        if not ticket_id:
            logger.info("No ticket ID found, ignoring.")
            return jsonify({"status": "ignored"}), 200

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