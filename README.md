# Gorgias × Claude Webhook

Automatically analyzes new Gorgias support tickets with Claude Haiku and posts an internal note with a summary, category, urgency, best macro match, and draft response — before your agents open the ticket.

---

## How it works

1. A new ticket is created in Gorgias → webhook fires to your server
2. Server fetches full ticket details + your macros from the Gorgias API
3. Claude Haiku analyzes the ticket
4. Server posts an internal note back to the ticket with Claude's analysis

---

## Setup

### 1. Get your Gorgias API credentials

1. Log in to Gorgias and go to **Settings → REST API**
2. Copy your **API key**
3. Note the **email** tied to your account
4. Your domain is `yourstore.gorgias.com` (the subdomain in your Gorgias URL)

### 2. Get your Anthropic API key

1. Go to [https://console.anthropic.com/settings/api-keys](https://console.anthropic.com/settings/api-keys)
2. Click **Create Key** and copy it

### 3. Set up environment variables

```bash
cp .env.example .env
# Edit .env and fill in all four values
```

### 4. Run locally (for testing)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Load env vars and start server
export $(cat .env | xargs)
python app.py
```

The server starts at `http://localhost:5000`. Use a tool like [ngrok](https://ngrok.com) to expose it temporarily for webhook testing:

```bash
ngrok http 5000
# Copy the https://xxxx.ngrok.io URL for the Gorgias webhook below
```

---

## Deploy to Railway (free tier)

### 1. Create a Railway account

Sign up at [https://railway.app](https://railway.app) (free tier, no credit card required for low usage).

### 2. Deploy from GitHub

1. Push this project to a GitHub repository:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create gorgias-claude-webhook --public --push --source=.
   ```
2. In Railway, click **New Project → Deploy from GitHub repo**
3. Select your repository
4. Railway will auto-detect Python and deploy using `gunicorn`

### 3. Add environment variables in Railway

1. In your Railway project, go to **Variables**
2. Add each variable from `.env.example`:
   - `GORGIAS_DOMAIN`
   - `GORGIAS_EMAIL`
   - `GORGIAS_API_KEY`
   - `ANTHROPIC_API_KEY`

### 4. Add a start command (if needed)

If Railway doesn't auto-detect the start command, go to **Settings → Start Command** and set:

```
gunicorn app:app
```

### 5. Get your public URL

After deployment, Railway gives you a URL like `https://gorgias-claude-webhook-production.up.railway.app`. Copy it.

You can verify the server is running by visiting `https://your-railway-url.up.railway.app/` — it should return:
```json
{"status": "ok", "service": "gorgias-claude-webhook"}
```

---

## Connect the Gorgias webhook

1. In Gorgias, go to **Settings → Integrations → HTTP**
2. Click **Add HTTP Integration**
3. Configure it:
   - **Name:** Claude Ticket Analyzer
   - **URL:** `https://your-railway-url.up.railway.app/webhook/gorgias`
   - **Method:** POST
   - **Trigger:** `ticket-created` and/or `ticket-message-created`
4. Save and enable the integration

From now on, every new ticket will automatically get a Claude internal note within seconds.

---

## Internal note format

```
🤖 Claude Analysis

Summary: Customer is requesting a refund for order #12345 placed last week.
Category: billing
Urgency: medium
Best Macro: Refund Request - Standard

Draft Response:
Hi [Name], thanks for reaching out! I can help you with your refund request for order #12345...
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Webhook returns 500 | Check Railway logs for the Python traceback |
| Internal note not appearing | Verify `GORGIAS_DOMAIN`, `GORGIAS_EMAIL`, `GORGIAS_API_KEY` are correct |
| Claude not responding | Check `ANTHROPIC_API_KEY` and your Anthropic account credits |
| Notes triggering more notes | The app skips `internal-note` channel messages — confirm your note is posted on that channel |
| Gorgias keeps retrying | The app always returns `200` — if Gorgias retries, check the event type filter |
