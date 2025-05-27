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
from langchain_community.document_loaders import TextLoader
from agents import LogAnalyzer, CodeFixer
from supabase_service import check_if_repo_indexed, search_relevant_code_chunks
from repository_indexer import process_repository

load_dotenv()

APP_ID = os.getenv("APP_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if PRIVATE_KEY:
    with open(PRIVATE_KEY, "r") as f:
        PRIVATE_KEY = f.read()

app = FastAPI()


@app.on_event("startup")
async def start_polling():
    print("start polling")
    asyncio.create_task(poll_loop())


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
                                log_content_str = log_content_bytes.decode('latin-1', errors='replace')  # Fallback

                            # Save the raw content to a temporary file for TextLoader
                            # (TextLoader expects a file path)
                            # We can use the final path directly if we write, then load, then overwrite.
                            with open(output_log_path, "w", encoding="utf-8") as f:
                                f.write(log_content_str)

                            # Process with LangChain TextLoader
                            loader = TextLoader(str(output_log_path), encoding="utf-8")
                            docs = loader.load()  # This might split the doc, which is fine.

                            # Overwrite with preprocessed content (or content from TextLoader)
                            # TextLoader usually provides one document per file, but it could be chunked.
                            # For simplicity, we'll join them back if chunked, or just write the content.
                            with open(output_log_path, "w", encoding="utf-8") as f:
                                for doc in docs:
                                    f.write(doc.page_content + "\n")  # Add newline between docs if chunked
            print(f"Processed and stored logs for failed run {run_id} in {run_specific_dir}")
        else:
            print(f"Failed to fetch logs: {resp.status_code} {resp.text}")


async def get_github_file_details(owner: str, repo: str, path: str, ref: str, installation_id: str) -> dict | None:
    """Fetches file details (including content and SHA) from a repository.
       Returns the JSON response from GitHub API or None if an error occurs.
    """
    token = await get_installation_access_token(installation_id)
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()  # Raise an exception for 4xx/5xx errors
            return resp.json()  # Return the full JSON response
        except httpx.HTTPStatusError as e:
            print(
                f"[GitHub API Error] Failed to get file details for {path} at ref {ref} in {owner}/{repo}. Status: {e.response.status_code}, Response: {e.response.text}")
            return None
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while getting file details for {path}: {e}")
            return None


async def create_github_branch(owner: str, repo: str, new_branch_name: str, from_sha: str, installation_id: str):
    token = await get_installation_access_token(installation_id)
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
        resp.raise_for_status()  # Raise an exception for bad status codes
        print(f"[GitHub API] Successfully created branch {new_branch_name} from SHA {from_sha}")
        return resp.json()


async def delete_github_branch(owner: str, repo: str, branch_name: str, installation_id: str):
    token = await get_installation_access_token(installation_id)
    # Note: The ref is heads/{branch_name}, not just branch_name
    url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.delete(url, headers=headers)
            if resp.status_code == 404 or resp.status_code == 422:  # 404 or 422 (if ref doesn't exist for deletion)
                print(
                    f"[GitHub API] Branch {branch_name} did not exist or couldn't be deleted (Status: {resp.status_code}). Safe to proceed with creation.")
            else:
                resp.raise_for_status()  # Raise for other errors like 403, 500 etc.
                print(f"[GitHub API] Successfully deleted branch {branch_name}.")
        except httpx.HTTPStatusError as e:
            # If it's a 404 or 422 (ref not found), it's fine, means branch doesn't exist.
            if e.response.status_code == 404 or e.response.status_code == 422:
                print(
                    f"[GitHub API] Branch {branch_name} not found during delete attempt (Status: {e.response.status_code}). Safe to create.")
            else:
                print(
                    f"[GitHub API Error] Failed to delete branch {branch_name}. Status: {e.response.status_code}, Response: {e.response.text}")
                # Decide if we want to raise here or let creation fail. 
                # For now, let's log and let creation attempt proceed, which might then fail more clearly.
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while deleting branch {branch_name}: {e}")


async def commit_file_to_github(owner: str, repo: str, branch_name: str, file_path: str, file_content: str,
                                commit_message: str, installation_id: str, original_file_sha: str):
    token = await get_installation_access_token(installation_id)
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
        "sha": original_file_sha
    }
    # Check if file exists to get its SHA (for updating)
    # For simplicity, this example always creates/overwrites. 
    # A more robust version would get SHA if file exists for the "sha" field in data.
    async with httpx.AsyncClient() as client:
        resp = await client.put(url, headers=headers, json=data)
        resp.raise_for_status()
        print(f"[GitHub API] Successfully committed file {file_path} to branch {branch_name}")
        return resp.json()


async def create_github_pull_request_api(owner: str, repo: str, head_branch: str, base_branch: str, title: str,
                                         body: str, installation_id: str):
    token = await get_installation_access_token(installation_id)
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
        resp.raise_for_status()
        print(f"[GitHub API] Successfully created Pull Request: '{title}'")
        return resp.json()


def parse_diff_to_files(diff_text: str) -> list[dict[str, str]]:
    """Parses a combined diff into individual file diffs."""
    file_diffs = []
    # Split by the --- a/file_path line, keeping the delimiter
    raw_file_blocks = re.split(r'(--- a/.*?\n)', diff_text)
    current_file_path = None
    current_diff_block = []

    if not raw_file_blocks:
        return []

    # The first element might be empty if the diff starts with --- a/
    # or it could be preamble if the diff is not perfectly clean.
    idx = 0
    if not raw_file_blocks[0].strip() and len(raw_file_blocks) > 1:
        idx = 1  # Start from the first actual delimiter if the first block is empty

    # Check if the very first line is a diff header
    if raw_file_blocks[0].startswith("--- a/"):
        # Handle the case where the diff starts immediately with a file header
        # and re.split might not put the delimiter first.
        # This logic assumes a fairly clean diff input.
        current_file_path_match = re.match(r'--- a/(.*?)\n', raw_file_blocks[0])
        if current_file_path_match:
            current_file_path = current_file_path_match.group(1)
            current_diff_block.append(raw_file_blocks[0])
            if len(raw_file_blocks) > 1 and not raw_file_blocks[1].startswith("--- a/"):
                # Content of the first file if split worked as (delim, content, delim, content ...)
                current_diff_block.append(raw_file_blocks[1])
                idx = 2
            else:
                idx = 1  # Only header was present, or next is another header
        else:  # First block is preamble
            if len(raw_file_blocks) > 1:  # If there is more after preamble
                idx = 1  # Skip preamble
            else:  # Only preamble
                return []
    elif len(raw_file_blocks) > 1:  # Preamble, then a delimiter
        idx = 1
    else:  # Only preamble, no diffs
        return []

    while idx < len(raw_file_blocks):
        delimiter = raw_file_blocks[idx]
        content = raw_file_blocks[idx + 1] if idx + 1 < len(raw_file_blocks) else ""

        if current_file_path:
            file_diffs.append({
                "file_path": current_file_path,
                "diff_content": "".join(current_diff_block).strip()
            })
            current_diff_block = []

        file_path_match = re.match(r'--- a/(.*?)\n', delimiter)
        if file_path_match:
            current_file_path = file_path_match.group(1)
            current_diff_block.append(delimiter)
            if not content.startswith("--- a/"):
                current_diff_block.append(content)
                idx += 2
            else:
                idx += 1  # Next is a delimiter, current content block is empty
        else:  # Should not happen if split correctly, but as a safeguard
            idx += 1  # Move to next block

    # Add the last processed file
    if current_file_path and current_diff_block:
        file_diffs.append({
            "file_path": current_file_path,
            "diff_content": "".join(current_diff_block).strip()
        })
    return file_diffs


def apply_patch_to_content(original_content: str, diff_text: str) -> str:
    """Applies a given diff/patch to the original file content.
       This is a simplified patch applicator.
       It assumes a standard diff format and sequential application.
    """
    original_lines = original_content.splitlines()
    patched_lines = []
    diff_lines = diff_text.splitlines()

    original_line_idx = 0
    diff_line_idx = 0

    while diff_line_idx < len(diff_lines):
        current_diff_line = diff_lines[diff_line_idx]

        if current_diff_line.startswith("--- a/") or current_diff_line.startswith("+++ b/"):
            diff_line_idx += 1
            continue
        elif current_diff_line.startswith("@@"):
            # Parse hunk header like "@@ -original_start,original_length +patched_start,patched_length @@"
            match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', current_diff_line)
            if not match:
                # Invalid hunk header, cannot apply patch, return original or raise error
                # For simplicity, let's skip this diff and try to append remaining original
                print(f"[WARN] Invalid hunk header: {current_diff_line}. Skipping patch application for this hunk.")
                break

            original_start_line = int(match.group(1)) - 1  # 0-indexed
            # original_length = int(match.group(2) or 1)
            # patched_start_line = int(match.group(3)) - 1 # 0-indexed
            # patched_length = int(match.group(4) or 1)

            # Add lines from original content before this hunk starts
            while original_line_idx < original_start_line:
                if original_line_idx < len(original_lines):
                    patched_lines.append(original_lines[original_line_idx])
                original_line_idx += 1

            diff_line_idx += 1  # Move to content lines of the hunk

            # Process lines within the hunk
            while diff_line_idx < len(diff_lines) and not diff_lines[diff_line_idx].startswith("@@"):
                hunk_line = diff_lines[diff_line_idx]
                if hunk_line.startswith('+'):
                    patched_lines.append(hunk_line[1:])
                    diff_line_idx += 1
                elif hunk_line.startswith('-'):
                    original_line_idx += 1  # Consume from original, matched line is skipped from patched_lines
                    diff_line_idx += 1
                else:  # Context line
                    # Normalize context from diff: strip leading space if present, then general strip.
                    expected_context_from_diff = hunk_line[1:] if hunk_line.startswith(' ') else hunk_line
                    expected_context_from_diff = expected_context_from_diff.rstrip()  # rstrip to handle potential trailing spaces without affecting content comparison much

                    if original_line_idx < len(original_lines):
                        actual_original_line = original_lines[original_line_idx].rstrip()
                        # Strict comparison for context lines
                        if actual_original_line == expected_context_from_diff:
                            patched_lines.append(original_lines[
                                                     original_line_idx])  # Append the true original line with its original spacing
                            original_line_idx += 1
                            diff_line_idx += 1
                        else:
                            print(f"[ERROR] Patch context mismatch at original file line {original_line_idx + 1}:")
                            print(f"  Diff context: '{expected_context_from_diff}'")
                            print(f"  Original file: '{actual_original_line}'")
                            print(f"  Aborting patch for this file due to mismatch. Hunk: {current_diff_line}")
                            return "PATCH_APPLICATION_FAILED_CONTEXT_MISMATCH"
                    else:
                        print(
                            f"[ERROR] Diff context line implies original content beyond its actual length. Original lines: {len(original_lines)}, current index: {original_line_idx}. Diff line: '{hunk_line}'")
                        return "PATCH_APPLICATION_FAILED_CONTEXT_MISMATCH"  # Abort
        else:
            # Line is not a header or hunk, should not happen in clean diff
            # For robustness, we can skip it or log a warning.
            diff_line_idx += 1
            continue

    # Add any remaining lines from the original file
    while original_line_idx < len(original_lines):
        patched_lines.append(original_lines[original_line_idx])
        original_line_idx += 1

    return "\n".join(patched_lines)


@app.post("/api/webhook")
async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_github_event: str = Header(None),
        x_hub_signature_256: str = Header(None)
):
    print(f"Received event: {x_github_event}")
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    repo_info = payload.get("repository", {})
    owner = repo_info.get("owner", {}).get("login")
    repo_name = repo_info.get("name")
    installation_id = payload.get("installation", {}).get("id")

    # It's good practice to ensure these are present early if they are essential for all or most events.
    # For now, specific checks are done within event handlers if an operation depends on them.

    if x_github_event == "installation" and payload.get("action") in ["created", "new_permissions_accepted"]:
        installation_id = payload.get("installation", {}).get("id")
        if payload.get("repositories"):
            for repo_data in payload.get("repositories", []):
                repo_full_name = repo_data.get("full_name")  # e.g., "owner/repo"
                if repo_full_name and installation_id:
                    r_owner, r_name = repo_full_name.split('/')
                    repo_url = f"https://github.com/{repo_full_name}.git"
                    print(f"[installation created/permissions accepted] Processing repository: {repo_full_name}")
                    # Get token for this specific installation to clone
                    install_token = await get_installation_access_token(installation_id)
                    background_tasks.add_task(process_repository, repo_url, repo_full_name, r_owner, r_name,
                                              installation_token=install_token)
        else:  # All repositories selected for the installation
            # This case is harder to handle directly as it doesn't list all repos here.
            # Best to rely on `installation_repositories` or have user trigger indexing manually.
            # Or, you could list all repos for the installation via API if this event is critical.
            print(
                f"[installation created/permissions accepted] App installed on all repositories for {payload.get('installation', {}).get('account', {}).get('login')}. Consider using 'installation_repositories' event for individual repo processing or manual trigger.")

    elif x_github_event == "installation_repositories" and payload.get("action") == "added":
        installation_id = payload.get("installation", {}).get("id")
        if payload.get("repositories_added"):
            for repo_data in payload.get("repositories_added", []):
                repo_full_name = repo_data.get("full_name")  # e.g., "owner/repo"
                if repo_full_name and installation_id:
                    r_owner, r_name = repo_full_name.split('/')
                    repo_url = f"https://github.com/{repo_full_name}.git"
                    print(f"[installation_repositories added] Processing repository: {repo_full_name}")
                    install_token = await get_installation_access_token(installation_id)
                    background_tasks.add_task(process_repository, repo_url, repo_full_name, r_owner, r_name,
                                              installation_token=install_token)

    elif x_github_event == "push":
        repo_info = payload.get("repository", {})  # Ensure repo_info is always accessed first
        owner = repo_info.get("owner", {}).get("login")
        repo_name = repo_info.get("name")
        installation_id = payload.get("installation", {}).get("id")  # May not always be present for all push types

        ref_payload = payload.get("ref", "")
        target_branch = ref_payload.split("/")[-1] if ref_payload else ""
        default_branch = repo_info.get("default_branch")

        # Condition 1: Push to default branch (triggers re-indexing)
        # Requires owner, repo_name, installation_id, and branches to be valid
        if owner and repo_name and installation_id and target_branch and default_branch and target_branch == default_branch:
            print(f"[push] Push to default branch {default_branch} of {owner}/{repo_name}")
            repo_full_name = f"{owner}/{repo_name}"
            repo_url = f"https://github.com/{repo_full_name}.git"
            print(f"[push] Triggering re-indexing for {repo_full_name}.")
            install_token = await get_installation_access_token(installation_id)
            background_tasks.add_task(process_repository, repo_url, repo_full_name, owner, repo_name,
                                      branch=default_branch, installation_token=install_token)

        # Condition 2: Generic push message (logs commits), if not a default branch push or if missing details for re-indexing
        elif owner and repo_name:
            print(f"[push] New commit(s) pushed to {owner}/{repo_name} (branch: {target_branch}):")
            commits = payload.get("commits", [])
            for commit in commits:
                print(f"  - {commit.get('id')[:7]}: {commit.get('message')}")

        # Condition 3: Incomplete information for any meaningful push processing
        else:
            print("[push] Received push event with incomplete repository/owner information.")

    elif x_github_event == "workflow_run":
        action = payload.get("action")
        workflow_run_info = payload.get("workflow_run", {})
        status = workflow_run_info.get("status")
        conclusion = workflow_run_info.get("conclusion")
        run_id = workflow_run_info.get("id")

        if not all([action, status, run_id, owner, repo_name, installation_id]):
            print("Error: Incomplete workflow_run payload. Skipping processing.")
            return {"ok": False, "error": "Incomplete payload"}

        if action == "requested":
            print(f"Workflow run started: {run_id} in {owner}/{repo_name}")

        negative_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
        if status == "completed" and conclusion in negative_conclusions:
            print(f"Workflow run ended with negative status '{conclusion}': {run_id} in {owner}/{repo_name}")

            # Initialize variables for CodeFixer, ensuring they are always defined
            workflow_yaml_content = ""
            target_branch_for_fix = "unknown"
            workflow_path = None
            current_head_sha = None

            try:
                workflow_info = payload.get("workflow", {})
                workflow_path = workflow_info.get("path")  # e.g., .github/workflows/main.yml

                # Get head_sha and head_branch from workflow_run_info (which is payload.get("workflow_run", {}))
                current_head_sha = workflow_run_info.get("head_sha")
                current_head_branch = workflow_run_info.get("head_branch")

                if owner and repo_name and workflow_path and current_head_sha and installation_id:
                    print(
                        f"[INFO] Attempting to fetch workflow YAML '{workflow_path}' from '{owner}/{repo_name}' at SHA '{current_head_sha}'")
                    workflow_file_details = await get_github_file_details(
                        owner, repo_name, workflow_path, current_head_sha, installation_id
                    )
                    if workflow_file_details and workflow_file_details.get("content") and workflow_file_details.get(
                            "encoding") == "base64":
                        workflow_yaml_content = base64.b64decode(workflow_file_details["content"]).decode('utf-8')
                        print(f"[INFO] Fetched workflow YAML. Length: {len(workflow_yaml_content)} chars.")
                    elif workflow_file_details:  # Content not base64 or missing, or other issue
                        print(
                            f"[WARN] Fetched workflow YAML details, but content was not base64 encoded or was missing: {workflow_file_details}")
                        workflow_yaml_content = ""  # Fallback to empty
                    else:
                        print(f"[WARN] Failed to fetch workflow YAML details for {workflow_path}.")
                        workflow_yaml_content = ""  # Fallback to empty
                else:
                    print(f"[WARN] Could not attempt to fetch workflow YAML. Missing one or more required components:")
                    if not owner: print("[WARN] Missing: owner")
                    if not repo_name: print("[WARN] Missing: repo_name")
                    if not workflow_path: print(f"[WARN] Missing: workflow_path (from payload.workflow.path)")
                    if not current_head_sha: print(
                        f"[WARN] Missing: current_head_sha (from payload.workflow_run.head_sha)")
                    if not installation_id: print(f"[WARN] Missing: installation_id")

                if current_head_branch:
                    target_branch_for_fix = current_head_branch
                    print(f"[INFO] Target branch for fix identified as: {target_branch_for_fix}")
                else:
                    print(
                        "[WARN] head_branch is missing from payload.workflow_run.head_branch. Target branch will remain '{target_branch_for_fix}'.")

            except Exception as e_wf_fetch:
                print(f"[ERROR] Failed during attempt to fetch workflow YAML or identify target branch: {e_wf_fetch}")
                # workflow_yaml_content and target_branch_for_fix will retain their initial default values

            try:
                await fetch_and_store_workflow_logs(owner, repo_name, run_id, installation_id)
            except Exception as e:
                print(f"Error during fetch_and_store_workflow_logs: {e}")
                return {"ok": False, "error": "Failed to fetch/store logs"}  # Stop further processing

            log_directory_for_analysis = Path("logs") / f"{owner}_{repo_name}" / str(run_id)

            if os.getenv("GOOGLE_API_KEY"):
                try:
                    log_analyzer = LogAnalyzer()
                    codefixer_prompt_input = await log_analyzer.async_analyze_log_directory(
                        str(log_directory_for_analysis))

                    print("\n========== Structured Prompt for CodeFixer (from LogAnalyzer) ==========")
                    print(codefixer_prompt_input)
                    print("========== End of Structured Prompt for CodeFixer ==========")

                    if not codefixer_prompt_input.startswith("Error:"):
                        print("[DEBUG] Entered RAG/CodeFixer block.")  # DIAGNOSTIC 1

                        # RAG: Search for relevant code chunks
                        current_repo_id_for_search = f"{owner}/{repo_name}"
                        print(f"[DEBUG] current_repo_id_for_search: {current_repo_id_for_search}")  # DIAGNOSTIC

                        search_query_text = ""
                        print("[DEBUG] About to parse LogAnalyzer output for search query.")  # DIAGNOSTIC 2a
                        try:
                            problem_statement_start = codefixer_prompt_input.find("## Problem Statement")
                            key_snippets_start = codefixer_prompt_input.find("## Key Log Snippets")

                            if problem_statement_start != -1:
                                problem_statement_end = key_snippets_start if key_snippets_start > problem_statement_start else len(
                                    codefixer_prompt_input)
                                search_query_text = codefixer_prompt_input[problem_statement_start + len(
                                    "## Problem Statement"):problem_statement_end].strip()

                            if not search_query_text:
                                temp_query = codefixer_prompt_input.split('\\n')[1] if len(
                                    codefixer_prompt_input.split('\\n')) > 1 else codefixer_prompt_input
                                search_query_text = temp_query[:500]
                                print(
                                    f"Warning: Could not precisely parse Problem Statement for RAG query, using heuristic: '{search_query_text[:100]}...'")

                        except Exception as e_parse:
                            print(
                                f"Error parsing LogAnalyzer output for RAG query: {e_parse}. Using full output as fallback.")
                            search_query_text = codefixer_prompt_input

                        print(
                            f"[DEBUG] Parsed search_query_text (first 50 chars): '{search_query_text[:50]}' ")  # DIAGNOSTIC 2b

                        if search_query_text:
                            print(
                                f"[DEBUG] About to call search_relevant_code_chunks. Query: '{search_query_text[:200]}...'")  # DIAGNOSTIC 3
                            relevant_code_context = await search_relevant_code_chunks(
                                repo_id=current_repo_id_for_search,
                                query_text=search_query_text,
                                top_k=10,
                                similarity_threshold=0.3
                            )
                            print("[DEBUG] Called search_relevant_code_chunks.")  # DIAGNOSTIC 4a

                            if relevant_code_context:
                                print("[DEBUG] relevant_code_context IS NOT EMPTY.")  # DIAGNOSTIC 4b
                                print("\n========== RAG Retrieved Context Snippets (Top 10) ==========")
                                for i, context_chunk in enumerate(relevant_code_context):
                                    print(
                                        f"--- Snippet {i + 1} (Similarity: {context_chunk.get('similarity'):.4f}) ---")
                                    print(f"File: {context_chunk.get('file_path')}")
                                    print(f"Commit: {context_chunk.get('commit_sha')}")
                                print("========== End of RAG Retrieved Context Snippets ==========\n")

                                print("[DEBUG] About to instantiate CodeFixer.")  # DIAGNOSTIC 5
                                code_fixer = CodeFixer()
                                print(
                                    f"[DEBUG] CodeFixer instantiated. About to call propose_fix. workflow_yaml defined: {workflow_yaml_content is not None}, target_branch defined: {target_branch_for_fix is not None}")  # DIAGNOSTIC 6
                                proposed_fix_or_analysis = await code_fixer.propose_fix(
                                    log_analyzer_output=codefixer_prompt_input,
                                    relevant_code_snippets=relevant_code_context,
                                    workflow_yaml_content=workflow_yaml_content if workflow_yaml_content else "",
                                    workflow_yaml_path=workflow_path if workflow_path else "unknown_workflow_path.yml",
                                    target_branch=target_branch_for_fix if target_branch_for_fix else "unknown"
                                )
                                print("\n========== CodeFixer Agent Output ==========")
                                print(proposed_fix_or_analysis)
                                print("========== End of CodeFixer Agent Output ==========\n")

                                # Create a Pull Request with the proposed fix
                                if proposed_fix_or_analysis and "NO_CODE_FIX_POSSIBLE" not in proposed_fix_or_analysis and "```diff" in proposed_fix_or_analysis:
                                    if owner and repo_name and installation_id and run_id and target_branch_for_fix and target_branch_for_fix != "unknown" and current_head_sha:
                                        print(
                                            f"[INFO] Valid fix proposed. Attempting to apply changes and create a PR for run {run_id} in {owner}/{repo_name}.")

                                        new_branch_name = f"codefixer-apply-fix-{run_id}"
                                        pr_title = f"Apply CodeFixer proposed changes for Workflow Run {run_id}"
                                        base_branch_for_pr = "main"

                                        # Clean the diff from CodeFixer output
                                        cleaned_diff = proposed_fix_or_analysis.replace("```diff", "").replace("```",
                                                                                                               "").strip()
                                        individual_file_diffs = parse_diff_to_files(cleaned_diff)

                                        if not individual_file_diffs:
                                            print(
                                                "[WARN] Could not parse any file diffs from CodeFixer output. Skipping PR creation.")
                                        else:
                                            try:
                                                print(
                                                    f"[GITHUB_ACTION] Attempting to delete branch {new_branch_name} if it exists.")
                                                await delete_github_branch(
                                                    owner=owner,
                                                    repo=repo_name,
                                                    branch_name=new_branch_name,
                                                    installation_id=installation_id
                                                )
                                                # Proceed to create the branch even if delete had minor issues (like not found)

                                                print(
                                                    f"[GITHUB_ACTION] Creating branch: {new_branch_name} from SHA {current_head_sha}")
                                                await create_github_branch(
                                                    owner=owner,
                                                    repo=repo_name,
                                                    new_branch_name=new_branch_name,
                                                    from_sha=current_head_sha,
                                                    installation_id=installation_id
                                                )
                                                print(f"[GITHUB_ACTION] Successfully created branch {new_branch_name}.")

                                                modified_file_paths = []
                                                for file_diff_info in individual_file_diffs:
                                                    file_path_to_patch = file_diff_info["file_path"]
                                                    specific_diff_content = file_diff_info["diff_content"]
                                                    modified_file_paths.append(file_path_to_patch)

                                                    print(f"[DEBUG] Processing file for patching: {file_path_to_patch}")

                                                    # 1. Fetch original file content from the commit that failed
                                                    print(
                                                        f"[DEBUG] Fetching original file details for {file_path_to_patch} at SHA {current_head_sha}")
                                                    original_file_details = await get_github_file_details(
                                                        owner, repo_name, file_path_to_patch, current_head_sha,
                                                        installation_id
                                                    )
                                                    if not original_file_details or "content" not in original_file_details or "sha" not in original_file_details:
                                                        print(
                                                            f"[ERROR] Could not fetch original file details (content/SHA) for {file_path_to_patch} from ref {current_head_sha}. Skipping this file.")
                                                        continue

                                                    original_content_b64 = original_file_details["content"]
                                                    original_file_sha = original_file_details["sha"]

                                                    if original_file_details.get("encoding") != "base64":
                                                        print(
                                                            f"[ERROR] Original file {file_path_to_patch} encoding is not base64. Skipping this file.")
                                                        continue
                                                    try:
                                                        original_content_str = base64.b64decode(
                                                            original_content_b64).decode('utf-8')
                                                    except Exception as e_decode:
                                                        print(
                                                            f"[ERROR] Failed to decode base64 content for {file_path_to_patch}: {e_decode}. Skipping this file.")
                                                        continue

                                                    # 2. Apply the patch
                                                    print(f"[DEBUG] Applying patch to {file_path_to_patch}")
                                                    patched_content_str = apply_patch_to_content(original_content_str,
                                                                                                 specific_diff_content)

                                                    if patched_content_str == "PATCH_APPLICATION_FAILED_CONTEXT_MISMATCH":
                                                        print(
                                                            f"[ERROR] Failed to apply patch for {file_path_to_patch} due to context mismatch. Skipping commit for this file.")
                                                        # Optionally, remove from modified_file_paths if we don't want to list it in PR
                                                        if file_path_to_patch in modified_file_paths:
                                                            modified_file_paths.remove(
                                                                file_path_to_patch)  # So it won't be listed in PR body
                                                        continue  # Move to the next file_diff_info

                                                    # 3. Commit the patched file
                                                    commit_message_for_file = f"feat: Apply CodeFixer patch to {file_path_to_patch}"
                                                    print(
                                                        f"[DEBUG] Committing patched file {file_path_to_patch} to branch {new_branch_name}")
                                                    await commit_file_to_github(
                                                        owner=owner,
                                                        repo=repo_name,
                                                        branch_name=new_branch_name,
                                                        file_path=file_path_to_patch,
                                                        file_content=patched_content_str,  # Pass the string content
                                                        commit_message=commit_message_for_file,
                                                        installation_id=installation_id,
                                                        original_file_sha=original_file_sha
                                                        # Pass the original_file_sha
                                                    )
                                                    print(
                                                        f"[GITHUB_ACTION] Successfully committed patched {file_path_to_patch} to {new_branch_name}.")

                                                if not modified_file_paths:
                                                    print(
                                                        "[INFO] No files were successfully patched and committed. Skipping PR creation.")
                                                else:
                                                    pr_body_files_list = "\\n".join(
                                                        [f"- `{f}`" for f in modified_file_paths])
                                                    pr_body = (
                                                        f"This PR applies changes proposed by the CodeFixer agent for the workflow failure on branch `{target_branch_for_fix}` (triggered by commit `{current_head_sha[:7]}`).\\n\\n"
                                                        f"**Files Modified:**\\n{pr_body_files_list}\\n\\n"
                                                        f"Please review the applied changes.")

                                                    print(
                                                        f"[DEBUG] Creating PR: head={new_branch_name}, base={base_branch_for_pr}")
                                                    await create_github_pull_request_api(
                                                        owner=owner,
                                                        repo=repo_name,
                                                        head_branch=new_branch_name,
                                                        base_branch=base_branch_for_pr,
                                                        title=pr_title,
                                                        body=pr_body,
                                                        installation_id=installation_id
                                                    )
                                                    print(
                                                        f"[GITHUB_ACTION] Successfully created pull request: {pr_title}.")

                                            except ValueError as ve:  # Catch specific ValueError from current_head_sha check
                                                print(f"[ERROR] Skipping PR attempt due to ValueError: {ve}")
                                            except Exception as e_gh_pr:
                                                print(
                                                    f"[ERROR] Failed to apply changes and create GitHub pull request for run {run_id}: {e_gh_pr}")
                                                import traceback
                                                traceback.print_exc()
                                    else:
                                        print(
                                            f"[INFO] Skipping PR creation for run {run_id} due to missing information (owner, repo, installation_id, run_id, target_branch_for_fix, or current_head_sha).")
                                else:
                                    print(
                                        f"[INFO] No valid fix proposed by CodeFixer or fix was 'NO_CODE_FIX_POSSIBLE' for run {run_id}. Skipping PR creation.")
                            else:
                                print(
                                    f"LogAnalyzer returned an error, skipping RAG search and CodeFixer: {codefixer_prompt_input}")

                except Exception as e:
                    print(f"Error during LogAnalyzer, RAG search, or CodeFixer execution from app.py: {e}")
                    import traceback
                    traceback.print_exc()  # Print full traceback for debugging
            else:
                print("GOOGLE_API_KEY not set. Skipping LogAnalyzer and subsequent RAG/CodeFixer.")

    elif x_github_event == "pull_request" and payload.get("action") == "opened":
        pr_info = payload.get("pull_request", {})
        issue_number = pr_info.get("number")
        if owner and repo_name and installation_id and issue_number:
            message = "Thanks for opening a new PR! Please follow our contributing guidelines to make your PR easier to review."
            try:
                await post_pr_comment(owner, repo_name, issue_number, message, installation_id)
                print(f"Commented on PR #{issue_number}")
            except Exception as e:
                print(f"Failed to comment on PR: {e}")
        else:
            print("[pull_request.opened] Incomplete information to post comment.")

    return {"ok": True}
