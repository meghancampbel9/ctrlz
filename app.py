import os
import hmac
import hashlib
import time
import httpx
import jwt
import asyncio
import base64
import re
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
from dotenv import load_dotenv
import json
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
# from langchain_community.document_loaders import TextLoader
from agents import LogAnalyzer, CodeFixer
from supabase_service import check_if_repo_indexed, search_relevant_code_chunks, get_full_file_content_from_chunks, \
    get_supabase_client, get_embeddings_model  # Added imports
from repository_indexer import process_repository
from docker_app_motinor import poll_loop

load_dotenv()

APP_ID = os.getenv("APP_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY")
PRIVATE_KEY = None

if PRIVATE_KEY_PATH:
    try:
        with open(PRIVATE_KEY_PATH, "r") as f:
            PRIVATE_KEY = f.read()
    except FileNotFoundError:
        print(
            f"[CRITICAL ERROR] Private key file not found at path: {PRIVATE_KEY_PATH}. Ensure the PRIVATE_KEY env var is set to a valid file path.")
    except Exception as e:
        print(f"[CRITICAL ERROR] Error reading private key file: {e}")

if not APP_ID or not WEBHOOK_SECRET or not PRIVATE_KEY:
    print(
        "[CRITICAL ERROR] APP_ID, WEBHOOK_SECRET, or PRIVATE_KEY is not configured. Application may not function correctly.")

# Initialize Supabase and Embeddings model status check
supabase = get_supabase_client()
embeddings_model = get_embeddings_model()
if not supabase:
    print("[CRITICAL ERROR] Supabase client could not be initialized. Check Supabase URL/Key.")
if not embeddings_model:
    print("[CRITICAL ERROR] Embeddings model could not be initialized. Check GOOGLE_API_KEY.")

app = FastAPI()

@app.on_event("startup")
async def start_polling():
    print("start polling")
    asyncio.create_task(poll_loop())

def verify_signature(payload: bytes, signature: str):
    if not WEBHOOK_SECRET:
        print("[ERROR] WEBHOOK_SECRET not configured. Cannot verify signature.")
        return False
    if not signature:
        print("[ERROR] Signature not provided in webhook.")
        return False
    try:
        sha_name, signature_hex = signature.split('=', 1)
    except ValueError:
        print(f"[ERROR] Malformed signature header: {signature}")
        return False
    if sha_name != 'sha256':
        print(f"[ERROR] Signature algorithm not sha256: {sha_name}")
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature_hex)


def generate_jwt():
    if not APP_ID or not PRIVATE_KEY:
        print("[ERROR] APP_ID or PRIVATE_KEY not available for JWT generation.")
        raise ValueError("APP_ID or PRIVATE_KEY not configured for JWT generation.")
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),  # 10 minutes validity
        "iss": APP_ID
    }
    encoded_jwt = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return encoded_jwt


async def get_installation_access_token(installation_id):
    jwt_token = generate_jwt()
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, headers=headers)
            resp.raise_for_status()
            return resp.json()["token"]
        except httpx.HTTPStatusError as e:
            print(
                f"[GitHub API Error] Failed to get installation access token for ID {installation_id}. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        except Exception as e:
            print(f"[GitHub API Error] Unexpected error getting installation token: {e}")
            raise


async def post_pr_comment(owner, repo, issue_number, body, installation_id):
    try:
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
            print(f"Successfully posted comment to PR/Issue #{issue_number} in {owner}/{repo}")
            return resp.json()
    except Exception as e:
        print(f"[GitHub API Error] Failed to post PR comment to {owner}/{repo} #{issue_number}: {e}")
        return None


async def fetch_and_store_workflow_logs(owner, repo, run_id, installation_id):
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for log fetching: {e_token}")
        return  # Cannot proceed without token

    logs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(logs_url, headers=headers)
            resp.raise_for_status()  # Check for HTTP errors first

            base_logs_dir = Path("logs")
            repo_specific_dir = base_logs_dir / f"{owner}_{repo}"
            run_specific_dir = repo_specific_dir / str(run_id)
            run_specific_dir.mkdir(parents=True, exist_ok=True)

            with ZipFile(BytesIO(resp.content)) as zip_file:
                for log_filename_in_zip in zip_file.namelist():
                    if log_filename_in_zip.endswith(".txt"):  # Process only .txt files
                        sanitized_log_filename = log_filename_in_zip.replace('/', '_')
                        output_log_path = run_specific_dir / sanitized_log_filename

                        with zip_file.open(log_filename_in_zip) as log_file:
                            log_content_bytes = log_file.read()
                            try:
                                log_content_str = log_content_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                log_content_str = log_content_bytes.decode('latin-1', errors='replace')  # Fallback

                            with open(output_log_path, "w", encoding="utf-8") as f:
                                f.write(log_content_str)  # Store raw log content directly
            print(f"Processed and stored raw logs for failed run {run_id} in {run_specific_dir}")

        except httpx.HTTPStatusError as e_http:
            print(f"Failed to fetch logs for run {run_id}: {e_http.response.status_code} {e_http.response.text}")
        except Exception as e_zip:
            print(f"Error processing zip file for logs of run {run_id}: {e_zip}")


async def get_github_file_details(owner: str, repo: str, path: str, ref: str, installation_id: str) -> dict | None:
    """Fetches file details (including content and SHA) from a repository."""
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for file details: {e_token}")
        return None

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"[GitHub API Info] File {path} not found at ref {ref} in {owner}/{repo}. (404)")
            else:
                print(
                    f"[GitHub API Error] Failed to get file details for {path} at ref {ref}. Status: {e.response.status_code}, Response: {e.response.text}")
            return None
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while getting file details for {path}: {e}")
            return None


async def create_github_branch(owner: str, repo: str, new_branch_name: str, from_sha: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for creating branch: {e_token}")
        raise  # Re-raise as this is critical for PR creation flow

    url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    data = {
        "ref": f"refs/heads/{new_branch_name}",
        "sha": from_sha
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=data)
        if resp.status_code == 422:
            print(
                f"[GitHub API Warn] Could not create branch {new_branch_name} (Status 422). It might already exist. Response: {resp.text}")
            # Consider this as non-fatal if branch already exists, but log it.
            # Subsequent operations (like commit) will use the existing branch.
            return resp.json()
        resp.raise_for_status()
        print(f"[GitHub API] Successfully created branch {new_branch_name} from SHA {from_sha}")
        return resp.json()


async def delete_github_branch(owner: str, repo: str, branch_name: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for deleting branch: {e_token}")
        return  # Non-critical if delete fails, might just mean branch wasn't there

    url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.delete(url, headers=headers)
            if resp.status_code == 404 or resp.status_code == 422:
                print(
                    f"[GitHub API Info] Branch {branch_name} not found or couldn't be deleted (Status: {resp.status_code}). Safe to proceed.")
            else:
                resp.raise_for_status()
                print(f"[GitHub API] Successfully deleted branch {branch_name}.")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404 or e.response.status_code == 422:
                print(
                    f"[GitHub API Info] Branch {branch_name} not found during delete (Status: {e.response.status_code}). Safe to proceed.")
            else:
                print(
                    f"[GitHub API Error] Failed to delete branch {branch_name}. Status: {e.response.status_code}, Response: {e.response.text}")
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while deleting branch {branch_name}: {e}")


async def commit_file_to_github(owner: str, repo: str, branch_name: str, file_path: str, file_content: str,
                                commit_message: str, installation_id: str, original_file_sha: str | None):
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for committing file: {e_token}")
        raise

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    content_bytes = file_content.encode('utf-8')
    content_base64 = base64.b64encode(content_bytes).decode('utf-8')
    data = {
        "message": commit_message,
        "content": content_base64,
        "branch": branch_name,
    }
    if original_file_sha:
        data["sha"] = original_file_sha

    async with httpx.AsyncClient() as client:
        resp = await client.put(url, headers=headers, json=data)
        if resp.status_code == 409:
            print(
                f"[GitHub API Error] Conflict (409) committing {file_path} to {branch_name}. SHA mismatch or branch issue? SHA Used: {original_file_sha}. Response: {resp.text}")
        elif resp.status_code == 422:
            print(
                f"[GitHub API Error] Unprocessable Entity (422) committing {file_path} to {branch_name}. File too large or other validation error. Response: {resp.text}")
        resp.raise_for_status()
        print(
            f"[GitHub API] Successfully {'created' if not original_file_sha else 'updated'} file {file_path} in branch {branch_name}")
        return resp.json()


async def delete_github_file(owner: str, repo: str, branch_name: str, file_path: str, commit_message: str,
                             installation_id: str, original_file_sha: str):
    if not original_file_sha:
        print(
            f"[INTERNAL ERROR] Cannot delete file {file_path} without its SHA. This indicates an issue in fetching file details before attempting delete.")
        raise ValueError(f"SHA is required to delete file {file_path}.")
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for deleting file: {e_token}")
        raise

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    data = {
        "message": commit_message,
        "branch": branch_name,
        "sha": original_file_sha
    }
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers, json=data)
        resp.raise_for_status()
        print(f"[GitHub API] Successfully deleted file {file_path} from branch {branch_name}")
        return resp.json()


async def create_github_pull_request_api(owner: str, repo: str, head_branch: str, base_branch: str, title: str,
                                         body: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
    except Exception as e_token:
        print(f"Failed to get installation token for creating PR: {e_token}")
        raise

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    data = {
        "title": title,
        "head": head_branch,
        "base": base_branch,
        "body": body
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=data)
        if resp.status_code == 422:
            print(
                f"[GitHub API Error] Could not create PR (Status 422). Base: {base_branch}, Head: {head_branch}. Title: {title}. Response: {resp.text}")
            if "No commits between" in resp.text or "A pull request already exists" in resp.text:
                print(
                    f"[GitHub API Info] PR creation failed possibly because no changes were made or PR already exists.")
                # This is not necessarily an error state for the bot, could be a successful "no-op" or duplicate run.
            # Depending on the exact 422 error, might not want to raise_for_status() if it's an expected "no changes" scenario.
            # For now, let's allow it to proceed and return the response.
            return resp.json()
        resp.raise_for_status()
        print(f"[GitHub API] Successfully created Pull Request: '{title}'")
        return resp.json()


def parse_codefixer_output_to_files(llm_output: str) -> list[dict[str, str]]:
    """
    Parses the CodeFixer LLM output (expected to be full file contents or delete instructions)
    into a list of operations.
    """
    operations = []
    # Pattern for ==DELETE FILE:/path/to/file==
    delete_pattern = re.compile(r"^==DELETE FILE:(?P<file_path>.*?)==$", re.MULTILINE)

    # Pattern for ==BEGIN FILE:/path/to/file== ... ==END FILE:/path/to/file==
    upsert_pattern = re.compile(
        r"^==BEGIN FILE:(?P<file_path_begin>.*?)==$(?P<content>.*?)^==END FILE:(?P<file_path_end>.*?)==$",
        re.MULTILINE | re.DOTALL)

    all_matches = []
    for match in delete_pattern.finditer(llm_output):
        all_matches.append({'type': 'delete', 'match': match})
    for match in upsert_pattern.finditer(llm_output):
        all_matches.append({'type': 'upsert', 'match': match})

    # Sort by start position to process in order of appearance in LLM output
    all_matches.sort(key=lambda x: x['match'].start())

    for item in all_matches:
        match_type = item['type']
        match_obj = item['match']  # Renamed from 'match' to avoid conflict with re.match

        file_path_str = ""
        if match_type == 'delete':
            file_path_str = match_obj.group("file_path").strip()
            if file_path_str:
                operations.append({"action": "delete", "file_path": file_path_str})
                print(f"[CodeFixer Output Parser] Parsed DELETE for: {file_path_str}")
            else:
                print(f"[CodeFixer Output Parser Warning] Found DELETE marker with empty file path.")

        elif match_type == 'upsert':
            file_path_begin_str = match_obj.group("file_path_begin").strip()
            file_path_end_str = match_obj.group("file_path_end").strip()
            content_str = match_obj.group("content")

            # Remove the first newline after BEGIN marker and last newline before END marker
            if content_str.startswith('\n'):
                content_str = content_str[1:]
            if content_str.endswith('\n'):  # Check before stripping if it was just a newline
                content_str = content_str[:-1]

            if file_path_begin_str and file_path_begin_str == file_path_end_str:
                operations.append({
                    "action": "upsert",
                    "file_path": file_path_begin_str,
                    "content": content_str  # Content is now preserved as is from LLM
                })
                print(
                    f"[CodeFixer Output Parser] Parsed UPSERT for: {file_path_begin_str} (Content length: {len(content_str)})")
            else:
                print(
                    f"[CodeFixer Output Parser Warning] Mismatched or empty file paths in BEGIN/END markers: BEGIN='{file_path_begin_str}', END='{file_path_end_str}'")

    if not operations and llm_output.strip() not in ["NO_CODE_FIX_POSSIBLE", ""] and not llm_output.strip().startswith(
            "# LLM_RESPONSE_UNEXPECTED_FORMAT"):
        # This warning is important if the LLM deviates from the expected structured output.
        print(
            f"[CodeFixer Output Parser Warning] No operations parsed from LLM output, and it's not a known special string. Output (first 500 chars): {llm_output[:500]}")

    return operations


@app.post("/api/webhook")
async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_github_event: str = Header(None),
        x_hub_signature_256: str = Header(None)
):
    print(f"Received event: {x_github_event}")

    raw_body = await request.body()
    if not verify_signature(raw_body, x_hub_signature_256):
        print("Error: Webhook signature verification failed.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    action = payload.get("action")
    installation_id = payload.get("installation", {}).get("id")
    repository = payload.get("repository", {})
    owner = repository.get("owner", {}).get("login")
    repo_name = repository.get("name")

    print(
        f"Processing webhook for: {owner}/{repo_name}, Event: {x_github_event}, Action: {action}, Installation ID: {installation_id}")

    if not all([owner, repo_name, installation_id]):
        print(
            f"Error: Missing owner, repo_name, or installation_id in payload. Event: {x_github_event}, Action: {action}")
        return {"ok": False, "error": "Incomplete payload for repository identification."}

    if x_github_event == "repository" and action == "created":
        print(f"New repository created: {owner}/{repo_name}. Triggering indexing.")
        # Ensure process_repository can handle potential None for supabase/embeddings if init failed
        if supabase and embeddings_model:
            background_tasks.add_task(process_repository, owner, repo_name, installation_id)
            return {"ok": True, "message": "Repository indexing initiated."}
        else:
            print("[ERROR] Supabase or Embeddings model not available. Cannot initiate repository indexing.")
            return {"ok": False, "error": "Backend services not ready for indexing."}


    elif x_github_event == "push":
        ref = payload.get("ref", "")
        default_branch = repository.get("default_branch", "main")
        if ref == f"refs/heads/{default_branch}":
            print(f"Push event to default branch {default_branch} for {owner}/{repo_name}. Triggering re-indexing.")
            if supabase and embeddings_model:
                background_tasks.add_task(process_repository, owner, repo_name, installation_id)
                return {"ok": True, "message": "Repository re-indexing due to push to default branch."}
            else:
                print("[ERROR] Supabase or Embeddings model not available. Cannot initiate repository re-indexing.")
                return {"ok": False, "error": "Backend services not ready for re-indexing."}
        else:
            print(f"Push event to ref {ref}. Not the default branch ({default_branch}). Skipping re-indexing.")
            return {"ok": True, "message": "Push to non-default branch, no re-indexing."}

    elif x_github_event == "workflow_run":
        action = payload.get("action")
        workflow_run_info = payload.get("workflow_run", {})
        status = workflow_run_info.get("status")
        conclusion = workflow_run_info.get("conclusion")
        run_id = workflow_run_info.get("id")

        if not all([action, status, run_id, owner, repo_name, installation_id]):
            print(f"Error: Incomplete workflow_run payload for {owner}/{repo_name}. Skipping processing.")
            return {"ok": False, "error": "Incomplete payload"}

        if action == "requested":
            print(f"Workflow run {run_id} in {owner}/{repo_name} has been requested.")
            return {"ok": True, "message": "Workflow run requested acknowledgement"}  # Acknowledge early

        negative_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
        if status == "completed" and conclusion in negative_conclusions:
            print(
                f"Workflow run {run_id} in {owner}/{repo_name} ended with negative status '{conclusion}'. Initiating CodeFixer.")

            workflow_yaml_content = ""
            target_branch_for_fix = "unknown"
            workflow_path = None
            current_head_sha = None  # SHA of the commit that triggered the workflow run

            try:
                workflow_info = payload.get("workflow", {})
                workflow_path = workflow_info.get("path")
                current_head_sha = workflow_run_info.get("head_sha")
                current_head_branch = workflow_run_info.get("head_branch")

                if not current_head_sha:
                    print(
                        f"[ERROR] Critical: `head_sha` missing from workflow_run payload for run {run_id}. Cannot proceed with fix.")
                    return {"ok": False, "error": "Missing head_sha from workflow_run payload."}

                if owner and repo_name and workflow_path and installation_id:  # current_head_sha already checked
                    print(
                        f"[INFO] Attempting to fetch workflow YAML '{workflow_path}' from '{owner}/{repo_name}' at SHA '{current_head_sha}'")
                    workflow_file_details = await get_github_file_details(
                        owner, repo_name, workflow_path, current_head_sha, installation_id
                    )
                    if workflow_file_details and workflow_file_details.get("content") and workflow_file_details.get(
                            "encoding") == "base64":
                        workflow_yaml_content = base64.b64decode(workflow_file_details["content"]).decode('utf-8')
                        print(
                            f"[INFO] Fetched workflow YAML '{workflow_path}'. Length: {len(workflow_yaml_content)} chars.")
                    else:
                        print(
                            f"[WARN] Failed to fetch or decode workflow YAML content for {workflow_path} at {current_head_sha}. Proceeding without workflow content for CodeFixer.")
                        workflow_yaml_content = ""  # Ensure it's an empty string if fetch fails
                else:
                    print(
                        f"[WARN] Could not attempt to fetch workflow YAML. Missing components (owner, repo, path, or install_id).")

                if current_head_branch:
                    target_branch_for_fix = current_head_branch
                    print(f"[INFO] Target branch for fix identified as: {target_branch_for_fix}")
                else:
                    # Fallback to default branch if head_branch is somehow missing
                    target_branch_for_fix = repository.get("default_branch", "main")
                    print(
                        f"[WARN] 'head_branch' missing in workflow_run payload for run {run_id}. Using default branch '{target_branch_for_fix}' as target for fix.")

            except Exception as e_wf_fetch:
                print(
                    f"[ERROR] Failed during attempt to fetch workflow YAML or identify target branch for run {run_id}: {e_wf_fetch}")
                # Still proceed, CodeFixer might work with less context.

            try:
                await fetch_and_store_workflow_logs(owner, repo_name, run_id, installation_id)
            except Exception as e:
                print(f"[ERROR] Failed to fetch/store workflow logs for run {run_id}: {e}")
                return {"ok": False, "error": "Failed to fetch/store logs"}  # Critical for LogAnalyzer

            log_directory_for_analysis = Path("logs") / f"{owner}_{repo_name}" / str(run_id)

            # Ensure core services are up before proceeding with agent logic
            if not (os.getenv("GOOGLE_API_KEY") and embeddings_model and supabase):
                print(
                    "[ERROR] Core services (Google API Key, Embeddings, Supabase) not initialized. Skipping CodeFixer.")
                return {"ok": False, "error": "Backend services not ready for CodeFixer."}

            try:
                log_analyzer = LogAnalyzer()
                codefixer_prompt_input = await log_analyzer.async_analyze_log_directory(str(log_directory_for_analysis))

                print("\n========== Structured Prompt for CodeFixer (from LogAnalyzer) ==========")
                print(codefixer_prompt_input[:1000] + (
                    "..." if len(codefixer_prompt_input) > 1000 else ""))  # Truncate for logs
                print("========== End of Structured Prompt for CodeFixer ==========")

                if codefixer_prompt_input.startswith("Error:"):
                    print(
                        f"LogAnalyzer returned an error for run {run_id}, skipping RAG and CodeFixer. Error: {codefixer_prompt_input}")
                    return {"ok": True,
                            "message": "LogAnalyzer error, CodeFixer skipped."}  # Acknowledge, but don't proceed to fix

                current_repo_id_for_search = f"{owner}/{repo_name}"
                search_query_text = ""
                try:
                    problem_statement_start = codefixer_prompt_input.find("## Problem Statement")
                    key_snippets_start = codefixer_prompt_input.find(
                        "## Key Log Snippets")  # Used as end marker for problem statement
                    if problem_statement_start != -1:
                        problem_statement_text_block = codefixer_prompt_input[
                                                       problem_statement_start + len("## Problem Statement"):]
                        if key_snippets_start > problem_statement_start:
                            problem_statement_text_block = problem_statement_text_block[:key_snippets_start - (
                                        problem_statement_start + len("## Problem Statement"))]
                        search_query_text = problem_statement_text_block.strip()

                    if not search_query_text:  # Fallback if parsing above fails
                        temp_query_lines = codefixer_prompt_input.splitlines()
                        # Skip the "## Problem Statement" header itself if present
                        start_line_index = 0
                        if temp_query_lines and temp_query_lines[0].strip() == "## Problem Statement":
                            start_line_index = 1
                        temp_query = "\n".join(
                            temp_query_lines[start_line_index: start_line_index + 5])  # Take a few lines
                        search_query_text = temp_query[:500].strip()  # Limit length
                        print(
                            f"Warning: Could not precisely parse 'Problem Statement' for RAG query. Using heuristic (first few lines): '{search_query_text[:100]}...'")
                except Exception as e_parse:
                    print(
                        f"Error parsing LogAnalyzer output for RAG query: {e_parse}. Using full output (truncated) as fallback.")
                    search_query_text = codefixer_prompt_input[:1000].strip()  # Limit length

                full_file_code_context_for_fixer = []
                if search_query_text:
                    print(
                        f"[DEBUG] RAG Step 1: Searching for relevant code CHUNKS. Query: '{search_query_text[:200]}...'")
                    relevant_chunks_from_rag = await search_relevant_code_chunks(
                        repo_id=current_repo_id_for_search,
                        query_text=search_query_text,
                        top_k=5,  # Configurable: number of most relevant chunks to consider
                        similarity_threshold=0.3  # Configurable: relevance threshold
                    )
                    if relevant_chunks_from_rag:
                        print(f"[DEBUG] RAG Step 1: Found {len(relevant_chunks_from_rag)} relevant chunk(s).")
                        processed_files_for_full_content = set()
                        print(f"[DEBUG] RAG Step 2: Fetching full file content for files identified in chunks.")
                        for chunk_info in relevant_chunks_from_rag:
                            file_path_from_chunk = chunk_info.get("file_path")
                            # IMPORTANT: Use the commit_sha from the chunk if available, as that's the version indexed.
                            # Fallback to current_head_sha (the failing commit) if chunk doesn't have it (should be rare).
                            commit_sha_for_file_content = chunk_info.get("commit_sha", current_head_sha)

                            if file_path_from_chunk and (
                            file_path_from_chunk, commit_sha_for_file_content) not in processed_files_for_full_content:
                                print(
                                    f"[DEBUG] RAG Step 2: Attempting to fetch full content for '{file_path_from_chunk}' at commit '{commit_sha_for_file_content}'")
                                full_content = await get_full_file_content_from_chunks(
                                    repo_id=current_repo_id_for_search,
                                    file_path=file_path_from_chunk,
                                    commit_sha=commit_sha_for_file_content
                                )
                                if full_content is not None:  # Explicitly check for None, as empty string is valid content
                                    full_file_code_context_for_fixer.append({
                                        "file_path": file_path_from_chunk,
                                        "chunk_content": full_content,  # Key expected by CodeFixer agent
                                        "commit_sha": commit_sha_for_file_content,  # For context
                                        "similarity": chunk_info.get("similarity")  # Original chunk similarity
                                    })
                                    print(
                                        f"[DEBUG] RAG Step 2: Successfully fetched full content for {file_path_from_chunk} (length {len(full_content)}).")
                                    processed_files_for_full_content.add(
                                        (file_path_from_chunk, commit_sha_for_file_content))
                                else:
                                    print(
                                        f"[WARN] RAG Step 2: Could not fetch full content for {file_path_from_chunk} at commit {commit_sha_for_file_content}. It will not be passed to CodeFixer.")
                            elif not file_path_from_chunk:
                                print(f"[WARN] RAG Step 2: Chunk info missing 'file_path': {chunk_info}")
                            # else: file already processed or path missing
                    else:
                        print("[DEBUG] RAG Step 1: No relevant chunks found by vector search.")
                else:
                    print("[WARN] No search query text derived for RAG. Skipping RAG search for CodeFixer.")

                if not full_file_code_context_for_fixer:
                    print(
                        "[INFO] RAG: No full file content could be retrieved for CodeFixer context (either no chunks found initially or full content fetch failed for all). CodeFixer will proceed without RAG context from codebase files.")

                code_fixer = CodeFixer()
                proposed_fix_or_analysis = await code_fixer.propose_fix(
                    log_analyzer_output=codefixer_prompt_input,
                    relevant_code_snippets=full_file_code_context_for_fixer,
                    workflow_yaml_content=workflow_yaml_content,  # Pass the fetched YAML content
                    workflow_yaml_path=workflow_path if workflow_path else "unknown_workflow_path.yml",
                    target_branch=target_branch_for_fix
                )
                print("\n========== CodeFixer Agent Output (Raw) ==========")
                print(proposed_fix_or_analysis[:2000] + ("..." if len(proposed_fix_or_analysis) > 2000 else ""))
                print("========== End of CodeFixer Agent Output (Raw) ==========\n")

                is_actionable_codefixer_output = (
                        proposed_fix_or_analysis and
                        proposed_fix_or_analysis.strip() != "NO_CODE_FIX_POSSIBLE" and
                        not proposed_fix_or_analysis.startswith("Error:") and
                        not proposed_fix_or_analysis.startswith("# LLM_RESPONSE_UNEXPECTED_FORMAT")
                )

                if is_actionable_codefixer_output:
                    if owner and repo_name and installation_id and run_id and target_branch_for_fix and current_head_sha:
                        print(
                            f"[INFO] Potentially actionable output from CodeFixer. Attempting to parse and apply changes for run {run_id} in {owner}/{repo_name}.")

                        new_branch_name = f"codefixer-run-{run_id}-{current_head_sha[:7]}"
                        pr_title = f"CodeFixer Auto-Fix for Workflow Run {run_id} (failed on {target_branch_for_fix})"
                        base_branch_for_pr = repository.get("default_branch", "main")
                        if base_branch_for_pr != target_branch_for_fix:
                            print(
                                f"[INFO] PR will be targeted at default branch '{base_branch_for_pr}', while failure was on '{target_branch_for_fix}'.")

                        file_operations = parse_codefixer_output_to_files(proposed_fix_or_analysis)

                        if not file_operations:
                            print(
                                f"[WARN] CodeFixer provided output, but parsing yielded no file operations. Output was: {proposed_fix_or_analysis[:500]}... Skipping PR creation.")
                        else:
                            try:
                                print(
                                    f"[GITHUB_ACTION] Attempting to delete branch {new_branch_name} if it exists (for idempotency).")
                                await delete_github_branch(owner, repo_name, new_branch_name, installation_id)

                                print(
                                    f"[GITHUB_ACTION] Creating new branch for fix: {new_branch_name} from SHA {current_head_sha}")
                                await create_github_branch(owner, repo_name, new_branch_name, current_head_sha,
                                                           installation_id)

                                committed_file_paths = []
                                deleted_file_paths = []

                                for op in file_operations:
                                    file_path_op = op["file_path"]  # Renamed to avoid conflict
                                    commit_action = op['action']
                                    commit_message = f"CodeFixer: {commit_action} {file_path_op} for workflow run {run_id}"

                                    original_file_sha_for_op = None
                                    # Fetch SHA for existing files (needed for update and delete)
                                    # Fetch against current_head_sha (the commit that failed) to get the correct blob SHA for that version
                                    print(
                                        f"[DEBUG] Fetching original file details for '{file_path_op}' at commit SHA '{current_head_sha}' (branch '{target_branch_for_fix}') for '{commit_action}' operation.")
                                    original_file_details = await get_github_file_details(
                                        owner, repo_name, file_path_op, current_head_sha, installation_id
                                    )
                                    if original_file_details and "sha" in original_file_details:
                                        original_file_sha_for_op = original_file_details["sha"]
                                        print(f"[DEBUG] Found SHA '{original_file_sha_for_op}' for '{file_path_op}'.")
                                    else:
                                        if commit_action == 'delete':
                                            print(
                                                f"[ERROR] Cannot delete '{file_path_op}': original file SHA not found (or file does not exist on branch '{target_branch_for_fix}' at commit '{current_head_sha}'). Skipping delete operation for this file.")
                                            continue  # Skip this delete operation
                                        else:  # Upsert for a new file if SHA not found
                                            print(
                                                f"[INFO] No SHA found for '{file_path_op}' during '{commit_action}'. Assuming new file creation.")
                                            original_file_sha_for_op = None  # Explicitly set to None for new file

                                    if commit_action == "upsert":
                                        file_content = op["content"]
                                        print(
                                            f"[DEBUG] Committing ('{commit_action}') file '{file_path_op}' (SHA: {'NEW' if not original_file_sha_for_op else original_file_sha_for_op}) to branch '{new_branch_name}'")
                                        await commit_file_to_github(
                                            owner, repo_name, new_branch_name, file_path_op,
                                            file_content, commit_message, installation_id,
                                            original_file_sha_for_op
                                        )
                                        committed_file_paths.append(file_path_op)

                                    elif commit_action == "delete":
                                        if original_file_sha_for_op:  # SHA is mandatory for delete
                                            print(
                                                f"[DEBUG] Deleting file '{file_path_op}' (SHA: {original_file_sha_for_op}) from branch '{new_branch_name}'")
                                            await delete_github_file(
                                                owner, repo_name, new_branch_name, file_path_op,
                                                commit_message, installation_id, original_file_sha_for_op
                                            )
                                            deleted_file_paths.append(file_path_op)
                                        # Else: error already logged above (SHA not found for delete), operation skipped.

                                if not committed_file_paths and not deleted_file_paths:
                                    print(
                                        "[INFO] No files were successfully committed or deleted by CodeFixer based on its output. Skipping PR creation.")
                                else:
                                    # These joins correctly use single \n and will produce strings with actual newlines
                                    pr_body_files_list_committed = "\n".join(
                                        [f"- Modified/Created: `{f}`" for f in committed_file_paths])
                                    pr_body_files_list_deleted = "\n".join(
                                        [f"- Deleted: `{f}`" for f in deleted_file_paths])

                                    files_summary_pr = ""
                                    if committed_file_paths:
                                        files_summary_pr += f"**Files Modified/Created:**\n{pr_body_files_list_committed}"  # Corrected to \n
                                    if deleted_file_paths:
                                        if files_summary_pr:
                                            files_summary_pr += "\n\n"  # Corrected to \n
                                        files_summary_pr += f"**Files Deleted:**\n{pr_body_files_list_deleted}"  # Corrected to \n

                                    problem_statement_for_pr = "Could not automatically parse the problem statement from logs."
                                    # Re-parse for PR body (keep it concise)
                                    if "## Problem Statement" in codefixer_prompt_input:
                                        try:
                                            problem_start_idx = codefixer_prompt_input.find(
                                                "## Problem Statement") + len("## Problem Statement")
                                            problem_end_idx = codefixer_prompt_input.find("## Key Log Snippets",
                                                                                          problem_start_idx)
                                            if problem_end_idx == -1: problem_end_idx = len(
                                                codefixer_prompt_input)  # if no key snippets
                                            parsed_problem = codefixer_prompt_input[
                                                             problem_start_idx:problem_end_idx].strip()
                                            if parsed_problem: problem_statement_for_pr = parsed_problem[:800] + (
                                                "..." if len(parsed_problem) > 800 else "")  # Limit length
                                        except Exception:
                                            pass

                                    fix_provided_summary_pr = "The CodeFixer agent proposed changes to the codebase."
                                    # Corrected pr_body construction with single \n for newlines
                                    pr_body = (
                                        f"This PR contains automated fixes proposed by the CodeFixer agent for a workflow failure on branch `{target_branch_for_fix}` "
                                        f"(triggered by commit `{current_head_sha[:7]}`).\n\n"
                                        f"**Original Problem Statement (from Log Analysis):**\n"
                                        f"> {problem_statement_for_pr.replace('<seg_7>', '\n> ')}\n\n"
                                        f"{files_summary_pr}\n\n"
                                        f"Please review the applied changes carefully."
                                    )

                                    print(
                                        f"[DEBUG] Creating PR: head='{new_branch_name}', base='{base_branch_for_pr}', title='{pr_title}'")
                                    await create_github_pull_request_api(
                                        owner, repo_name, new_branch_name, base_branch_for_pr,
                                        pr_title, pr_body, installation_id
                                    )
                                    print(f"[GITHUB_ACTION] Successfully created pull request: {pr_title}.")

                            except ValueError as ve:  # Catch specific ValueErrors, e.g. from delete_github_file if SHA missing
                                print(f"[ERROR] Skipping PR attempt due to ValueError: {ve}")
                            except Exception as e_gh_pr:
                                print(
                                    f"[ERROR] Failed to apply changes and create GitHub pull request for run {run_id}: {e_gh_pr}")
                                import traceback
                                traceback.print_exc()
                    else:  # Missing critical info for GitHub ops
                        print(
                            f"[INFO] Skipping PR creation for run {run_id} due to missing GitHub operational information (owner, repo, installation_id, current_head_sha, etc.).")

                elif proposed_fix_or_analysis and proposed_fix_or_analysis.strip() == "NO_CODE_FIX_POSSIBLE":
                    print(f"[INFO] CodeFixer determined no fix is possible for run {run_id}. No PR will be created.")
                    # Future: Consider creating a GitHub Issue here with the LogAnalyzer output

                elif proposed_fix_or_analysis and proposed_fix_or_analysis.startswith(
                        "# LLM_RESPONSE_UNEXPECTED_FORMAT"):
                    print(
                        f"[WARN] CodeFixer output for run {run_id} was in an unexpected format. No PR will be created. Details logged previously.")

                else:  # Error from CodeFixer or empty/unhandled response
                    print(
                        f"[INFO] No actionable output from CodeFixer for run {run_id}. Skipping PR creation. Response: '{proposed_fix_or_analysis}'")

            except Exception as e_agent_pipeline:  # Catch errors from agent pipeline
                print(
                    f"[CRITICAL] Unhandled error during LogAnalyzer, RAG, or CodeFixer execution for run {run_id}: {e_agent_pipeline}")
                import traceback
                traceback.print_exc()

        else:  # Workflow run not completed or did not fail negatively
            print(
                f"Workflow run {run_id} in {owner}/{repo_name} action '{action}', status '{status}', conclusion '{conclusion}'. No action taken by CodeFixer.")

    return {"ok": True}


# Health check endpoint
@app.get("/health")
async def health_check():
    # Basic health check, can be expanded (e.g., check Supabase/Google API connectivity)
    return {"status": "ok", "message": "CodeFixer service is running."}
