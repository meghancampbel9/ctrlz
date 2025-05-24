import os
import hmac
import hashlib
import time
import httpx
import jwt
from fastapi import FastAPI, Request, Header, HTTPException
from dotenv import load_dotenv
from pathlib import Path
from bot import poll_loop
from fastapi.responses import JSONResponse
import asyncio


load_dotenv()

APP_ID = os.getenv("APP_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

app = FastAPI()


def verify_signature(payload: bytes, signature: str):
    if not signature:
        return False
    sha_name, signature = signature.split('=')
    if sha_name != 'sha256':
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature)


def generate_jwt():
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": APP_ID
    }
    encoded_jwt = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return encoded_jwt


@app.on_event("startup")
async def start_polling():
    print("start polling")
    asyncio.create_task(poll_loop())

async def get_installation_access_token(installation_id):
    jwt_token = generate_jwt()
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json"
    }
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()["token"]


async def post_pr_comment(owner, repo, issue_number, body, installation_id):
    token = await get_installation_access_token(installation_id)
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    data = {"body": body}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=data)
        resp.raise_for_status()
        return resp.json()


async def fetch_and_store_workflow_logs(owner, repo, run_id, installation_id):
    token = await get_installation_access_token(installation_id)
    logs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(logs_url, headers=headers)
        if resp.status_code == 200:
            logs_dir = Path("logs")
            logs_dir.mkdir(exist_ok=True)
            log_path = logs_dir / f"{owner}_{repo}_run_{run_id}.zip"
            with open(log_path, "wb") as f:
                f.write(resp.content)
            print(f"Saved logs to {log_path}")
        else:
            print(f"Failed to fetch logs: {resp.status_code} {resp.text}")


async def read_file_from_repo(owner, repo, path, ref, installation_id):
    token = await get_installation_access_token(installation_id)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content = resp.json()
        if content.get("encoding") == "base64":
            import base64
            return base64.b64decode(content["content"]).decode()
        return content["content"]

@app.post("/api/webhook")

async def github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None)
):
    print(f"Received event: {x_github_event}")
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    # Handle push events (new commits)
    if x_github_event == "push":
        repo = payload["repository"]
        owner = repo["owner"]["login"]
        repo_name = repo["name"]
        commits = payload.get("commits", [])
        print(f"[push] New commit(s) pushed to {owner}/{repo_name}:")
        for commit in commits:
            print(f"  - {commit.get('id')[:7]}: {commit.get('message')}")

    # Handle workflow_run events
    if x_github_event == "workflow_run":
        action = payload.get("action")
        workflow_run = payload.get("workflow_run", {})
        status = workflow_run.get("status")
        conclusion = workflow_run.get("conclusion")
        run_id = workflow_run.get("id")
        repo = payload["repository"]
        owner = repo["owner"]["login"]
        repo_name = repo["name"]
        installation_id = payload["installation"]["id"]

        # Log when workflow is started
        if action == "requested":
            print(f"Workflow run started: {run_id} in {owner}/{repo_name}")
        # Fetch and store logs if workflow failed
        if status == "completed" and conclusion == "failure":
            print(f"Workflow run failed: {run_id} in {owner}/{repo_name}")
            await fetch_and_store_workflow_logs(owner, repo_name, run_id, installation_id)

    # Handle pull_request.opened
    if x_github_event == "pull_request" and payload.get("action") == "opened":
        pr = payload["pull_request"]
        repo = payload["repository"]
        installation_id = payload["installation"]["id"]
        owner = repo["owner"]["login"]
        repo_name = repo["name"]
        issue_number = pr["number"]
        message = "Thanks for opening a new PR! Please follow our contributing guidelines to make your PR easier to review."
        try:
            await post_pr_comment(owner, repo_name, issue_number, message, installation_id)
            print(f"Commented on PR #{issue_number}")
        except Exception as e:
            print(f"Failed to comment: {e}")

    return {"ok": True}The provided information shows a 500 error despite the JSON response indicating a "healthy" status. This is a mismatch and indicates a problem in the health check implementation itself, not necessarily a genuine failure of the application.  The `health()` function needs to be corrected to return the appropriate HTTP status code.

Here's a revised `health()` function (assuming a Python Flask application, adjust as needed for your framework):


```python
from flask import Flask, jsonify

app = Flask(__name__)

# Simulate a health check that might fail (replace with your actual health check logic)
def is_healthy():
    # Replace this with your actual health checks.  Examples:
    # - Check database connection
    # - Check external service availability
    # - Check file system access
    # - Check memory usage
    try:
        # Example: Check if a critical file exists
        with open("/tmp/healthcheck.txt", "r") as f:
            f.read()
        return True
    except FileNotFoundError:
        return False
    except Exception as e:  # Catch other potential errors
        print(f"Error during health check: {e}")
        return False


@app.route("/health")
def health():
    if is_healthy():
        return jsonify({"status": "healthy"}), 200
    else:
        return jsonify({"status": "unhealthy", "error": "Health check failed"}), 500

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000) # Adjust port as needed

```

**Explanation of Changes:**

1. **`is_healthy()` function:** This function encapsulates the actual health check logic.  It's crucial to replace the placeholder with your application's specific health checks.  The example shows a simple file existence check;  you'll need more robust checks based on your application's dependencies and critical components.  Error handling is included to prevent unexpected exceptions from crashing the health check.

2. **HTTP Status Codes:** The `health()` function now explicitly returns the correct HTTP status code (200 for healthy, 500 for unhealthy) along with the JSON response.  This ensures that monitoring systems correctly interpret the health check result.

3. **Error Message:**  A more informative error message is included in the 500 response to aid in debugging.  Consider logging the error details for better troubleshooting.

4. **Error Handling:** The `try...except` block in `is_healthy()` handles potential errors during the health check, preventing the entire application from crashing.  It's important to log these errors for later analysis.


**To use this:**

1.  **Replace the placeholder health check** in `is_healthy()` with your application's actual health checks.
2.  **Save the code** as a Python file (e.g., `health_check.py`).
3.  **Run the application:** `python health_check.py`
4.  **Test the health check endpoint:**  Access `/health` in your browser or using `curl`.


Remember to adjust the code to match your specific application framework and health check requirements.  If you provide more details about your application's architecture and dependencies, I can give you more tailored advice.