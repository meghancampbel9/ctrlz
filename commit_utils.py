import httpx
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

def get_github_commit_info(repo_name: str, branch: str = "main") -> tuple[str | None, str | None, dict | None]:
    """
    Get commit information from GitHub API.
    Returns a tuple of (commit_sha, commit_message, commit_details)
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Python/httpx",
        "Authorization": f"Bearer {GITHUB_TOKEN}"
    }
    
    # Get the latest commit
    url = f"https://api.github.com/repos/{repo_name}/commits/{branch}"
    try:
        response = httpx.get(url, headers=headers)
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch commit data: {response.status_code}")
            print(f"[ERROR] Response: {response.text}")
            return None, None, None
            
        commit_data = response.json()
        commit_sha = commit_data["sha"]
        commit_message = commit_data["commit"]["message"]
        
        # Get the commit details including diff
        commit_url = f"https://api.github.com/repos/{repo_name}/commits/{commit_sha}"
        commit_response = httpx.get(commit_url, headers=headers)
        
        if commit_response.status_code != 200:
            print(f"[ERROR] Failed to fetch commit diff: {commit_response.status_code}")
            print(f"[ERROR] Response: {commit_response.text}")
            return commit_sha, commit_message, None
            
        commit_details = commit_response.json()
        return commit_sha, commit_message, commit_details
        
    except Exception as e:
        print(f"[ERROR] Error fetching GitHub data: {str(e)}")
        return None, None, None

def get_commit_diff(repo_name: str) -> tuple[str | None, str | None, dict | None]:
    """
    Get commit information and diff from GitHub.
    Returns a tuple of (commit_sha, commit_message, commit_details)
    """
    commit_sha, commit_message, commit_details = get_github_commit_info(repo_name)
    return commit_sha, commit_message, commit_details

def print_commit_details(commit_sha: str, commit_message: str, commit_details: dict) -> None:
    """
    Print formatted commit details including SHA, message, and file changes.
    """
    print(f"[INFO] Latest commit in main: {commit_sha}")
    print(f"[INFO] Commit message: {commit_message}")
    print("\n[INFO] Code changes in this commit:")
    for file in commit_details["files"]:
        print(f"\nFile: {file['filename']}")
        print(f"Status: {file['status']}")
        if file['patch']:
            print("Changes:")
            print(file['patch'])
        print("-" * 50)

def fix_last_commit(app):
    repo_name = app["repo_name"]
    commit_sha, commit_message, commit_details = get_commit_diff(repo_name)
    
    if commit_sha and commit_details:
        print_commit_details(commit_sha, commit_message, commit_details)
        return commit_sha
    return None 