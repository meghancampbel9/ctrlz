import os
import hmac
import hashlib
import time
import httpx
import jwt
from fastapi import FastAPI, Request, Header, HTTPException
from dotenv import load_dotenv
import json
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
from langchain_community.document_loaders import TextLoader

load_dotenv()

APP_ID = os.getenv("APP_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if PRIVATE_KEY:
    with open(PRIVATE_KEY, "r") as f:
        PRIVATE_KEY = f.read()

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
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(logs_url, headers=headers)
        if resp.status_code == 200:
            # Create the structured directory path
            # logs/owner_repo/run_id/
            base_logs_dir = Path("logs")
            repo_specific_dir = base_logs_dir / f"{owner}_{repo}"
            run_specific_dir = repo_specific_dir / str(run_id)
            run_specific_dir.mkdir(parents=True, exist_ok=True)

            # Extract logs from zip in memory
            with ZipFile(BytesIO(resp.content)) as zip_file:
                for log_filename_in_zip in zip_file.namelist():
                    # Sanitize the filename from the zip to be a valid path component
                    # and ensure it's a .txt file
                    if log_filename_in_zip.endswith(".txt"):
                        # Use the original name from the zip, replacing / to avoid creating subdirs from it
                        sanitized_log_filename = log_filename_in_zip.replace('/', '_')
                        
                        # Path where the individual processed log file will be stored
                        output_log_path = run_specific_dir / sanitized_log_filename
                        
                        with zip_file.open(log_filename_in_zip) as log_file:
                            log_content_bytes = log_file.read()
                            try:
                                log_content_str = log_content_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                log_content_str = log_content_bytes.decode('latin-1', errors='replace') # Fallback

                            # Save the raw content to a temporary file for TextLoader
                            # (TextLoader expects a file path)
                            # We can use the final path directly if we write, then load, then overwrite.
                            with open(output_log_path, "w", encoding="utf-8") as f:
                                f.write(log_content_str)
                            
                            # Process with LangChain TextLoader
                            loader = TextLoader(str(output_log_path), encoding="utf-8")
                            docs = loader.load() # This might split the doc, which is fine.
                            
                            # Overwrite with preprocessed content (or content from TextLoader)
                            # TextLoader usually provides one document per file, but it could be chunked.
                            # For simplicity, we'll join them back if chunked, or just write the content.
                            with open(output_log_path, "w", encoding="utf-8") as f:
                                for doc in docs:
                                    f.write(doc.page_content + "\n") # Add newline between docs if chunked
            print(f"Processed and stored logs for failed run {run_id} in {run_specific_dir}")
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
        # Fetch and store logs if workflow ended with a negative status
        negative_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
        if status == "completed" and conclusion in negative_conclusions:
            print(f"Workflow run ended with negative status '{conclusion}': {run_id} in {owner}/{repo_name}")
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

    return {"ok": True}