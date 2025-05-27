import httpx
import base64
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO

from .auth import get_installation_access_token
from services.db.workflow_logs_service import store_workflow_log

async def post_pr_comment(owner: str, repo: str, issue_number: int, body: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
        if not token:
            print(f"[GitHub API Error] Failed to get token for posting PR comment to {owner}/{repo} #{issue_number}. Token was None.")
            return None
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

async def fetch_and_store_workflow_logs(owner: str, repo: str, run_id: int, installation_id: str, workflow_name_from_payload: str | None):
    try:
        token = await get_installation_access_token(installation_id)
        if not token:
            print(f"[GitHub API Error] Failed to get token for fetching logs. Token was None.")
            return False
    except Exception as e_token:
        print(f"Failed to get installation token for log fetching: {e_token}")
        return False # Cannot proceed without token

    logs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    repo_full_name = f"{owner}/{repo}"
    logs_stored_successfully = True

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(logs_url, headers=headers)
            resp.raise_for_status() # Check for HTTP errors first

            with ZipFile(BytesIO(resp.content)) as zip_file:
                for log_filename_in_zip in zip_file.namelist():
                    if log_filename_in_zip.endswith(".txt") and not log_filename_in_zip.startswith("__MACOSX"): # Process only .txt files
                        parts = Path(log_filename_in_zip).parts
                        job_name_from_log = None
                        if len(parts) > 1 and parts[-2] != '.':
                            job_name_from_log = parts[-2]
                        elif not workflow_name_from_payload:
                             job_name_from_log = Path(log_filename_in_zip).stem.split('_')[0]

                        current_workflow_name = workflow_name_from_payload
                        sanitized_log_filename = log_filename_in_zip.replace('/', '_')
                        
                        with zip_file.open(log_filename_in_zip) as log_file:
                            log_content_bytes = log_file.read()
                            try:
                                log_content_str = log_content_bytes.decode('utf-8')
                            except UnicodeDecodeError:
                                log_content_str = log_content_bytes.decode('latin-1', errors='replace')
                            
                            result = await store_workflow_log(
                                run_id=run_id,
                                repository_full_name=repo_full_name,
                                workflow_name=current_workflow_name,
                                job_name=job_name_from_log,
                                log_filename=sanitized_log_filename,
                                log_content=log_content_str
                            )
                            if result.get("error"):
                                print(f"Error storing log {sanitized_log_filename} to Supabase: {result.get('error')}")
                                logs_stored_successfully = False
            
            if logs_stored_successfully:
                print(f"Processed and initiated storage of logs for failed run {run_id} for repo {repo_full_name} in Supabase.")
            else:
                print(f"Some errors occurred while storing logs for run {run_id} for repo {repo_full_name} in Supabase.")
            return logs_stored_successfully

        except httpx.HTTPStatusError as e_http:
            print(f"Failed to fetch logs for run {run_id}: {e_http.response.status_code} {e_http.response.text}")
            return False
        except Exception as e_zip:
            print(f"Error processing zip file for logs of run {run_id}: {e_zip}")
            return False

async def get_github_file_details(owner: str, repo: str, path: str, ref: str, installation_id: str) -> dict | None:
    try:
        token = await get_installation_access_token(installation_id)
        if not token:
            print(f"[GitHub API Error] Failed to get token for file details. Token was None.")
            return None
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
                print(f"[GitHub API Error] Failed to get file details for {path} at ref {ref}. Status: {e.response.status_code}, Response: {e.response.text}")
            return None
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while getting file details for {path}: {e}")
            return None

async def create_github_branch(owner: str, repo: str, new_branch_name: str, from_sha: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
        if not token: 
            print(f"[GitHub API Error] Failed to get token for creating branch. Token was None.")
            raise ValueError("Token acquisition failed, cannot create branch.") 
    except Exception as e_token:
        print(f"Failed to get installation token for creating branch: {e_token}")
        raise
        
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
            print(f"[GitHub API Warn] Could not create branch {new_branch_name} (Status 422). It might already exist. Response: {resp.text}")
            return resp.json()
        resp.raise_for_status()
        print(f"[GitHub API] Successfully created branch {new_branch_name} from SHA {from_sha}")
        return resp.json()

async def delete_github_branch(owner: str, repo: str, branch_name: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
        if not token: 
            print(f"[GitHub API Info] Failed to get token for deleting branch {branch_name}. Token was None. Branch may not be deleted.")
            return
    except Exception as e_token:
        print(f"Failed to get installation token for deleting branch: {e_token}")
        return

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
                print(f"[GitHub API Info] Branch {branch_name} not found or couldn't be deleted (Status: {resp.status_code}). Safe to proceed.")
            else:
                resp.raise_for_status()
                print(f"[GitHub API] Successfully deleted branch {branch_name}.")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404 or e.response.status_code == 422:
                print(f"[GitHub API Info] Branch {branch_name} not found during delete (Status: {e.response.status_code}). Safe to proceed.")
            else:
                print(f"[GitHub API Error] Failed to delete branch {branch_name}. Status: {e.response.status_code}, Response: {e.response.text}")
        except Exception as e:
            print(f"[GitHub API Error] An unexpected error occurred while deleting branch {branch_name}: {e}")

async def commit_file_to_github(owner: str, repo: str, branch_name: str, file_path: str, file_content: str, commit_message: str, installation_id: str, original_file_sha: str | None):
    try:
        token = await get_installation_access_token(installation_id)
        if not token: 
            print(f"[GitHub API Error] Failed to get token for committing file. Token was None.")
            raise ValueError("Token acquisition failed, cannot commit file.")
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
             print(f"[GitHub API Error] Conflict (409) committing {file_path} to {branch_name}. SHA mismatch or branch issue? SHA Used: {original_file_sha}. Response: {resp.text}")
        elif resp.status_code == 422:
            print(f"[GitHub API Error] Unprocessable Entity (422) committing {file_path} to {branch_name}. File too large or other validation error. Response: {resp.text}")
        resp.raise_for_status()
        print(f"[GitHub API] Successfully {'created' if not original_file_sha else 'updated'} file {file_path} in branch {branch_name}")
        return resp.json()

async def delete_github_file(owner: str, repo: str, branch_name: str, file_path: str, commit_message: str, installation_id: str, original_file_sha: str):
    if not original_file_sha:
        print(f"[INTERNAL ERROR] Cannot delete file {file_path} without its SHA. This indicates an issue in fetching file details before attempting delete.")
        raise ValueError(f"SHA is required to delete file {file_path}.")
    try:
        token = await get_installation_access_token(installation_id)
        if not token: 
            print(f"[GitHub API Error] Failed to get token for deleting file. Token was None.")
            raise ValueError("Token acquisition failed, cannot delete file.")
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

async def create_github_pull_request_api(owner: str, repo: str, head_branch: str, base_branch: str, title: str, body: str, installation_id: str):
    try:
        token = await get_installation_access_token(installation_id)
        if not token: 
            print(f"[GitHub API Error] Failed to get token for creating PR. Token was None.")
            raise ValueError("Token acquisition failed, cannot create PR.")
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
            print(f"[GitHub API Error] Could not create PR (Status 422). Base: {base_branch}, Head: {head_branch}. Title: {title}. Response: {resp.text}")
            if "No commits between" in resp.text or "A pull request already exists" in resp.text:
                print(f"[GitHub API Info] PR creation failed possibly because no changes were made or PR already exists.")
            return resp.json()
        resp.raise_for_status()
        print(f"[GitHub API] Successfully created Pull Request: '{title}'")
        return resp.json() 