# CtrlZ
GitOps AI assistant acting as CtrlZ for production.

## Features
- Responds to GitHub webhook events (e.g., workflow runs, pull requests)
- Fetches and stores logs from failed workflow runs in the `/logs` directory
- Utility to read files from the repository (for future use)

## Install (Python/FastAPI)

1. **Clone the repository and enter the directory.**
2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Set environment variables

1. Create a `.env` file in the project root.
2. Add your GitHub App's private key, app ID, LLM APIO key, and webhook secret to the `.env` file:
   ```env
   APP_ID=your_github_app_id
   WEBHOOK_SECRET=your_webhook_secret
   PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----
   GOOGLE_API_KEY=your_api_key
   ```
   (Paste your private key as a single line, replacing newlines with `\n`.)

## Run the server

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```
- The webhook endpoint will be at `http://localhost:8000/api/webhook`.

## Expose to GitHub (for local development)

You can use [smee.io](https://smee.io/) to receive webhooks locally.


1 Set the smee channel URL as your webhook URL in your GitHub App settings.
3. On your local machine, run:
   ```bash
   npx smee-client --url https://smee.io/your-unique-channel --target http://localhost:8000/api/webhook
   ```
- Now, webhooks will be forwarded from smee.io to your local FastAPI server.

Learn more: [smee.io](https://smee.io/)
