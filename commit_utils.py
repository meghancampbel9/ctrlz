import httpx
import os
import requests
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

def fetch_deployment_logs() -> str:
    """
    Fetches the contents of deployment_logs/logs.txt and returns it as a string.
    """
    try:
        with open("deployment_logs/logs.txt", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[ERROR] Could not read deployment logs: {e}")
        return ""

llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash-latest", temperature=0.2)
deployment_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a DevOps expert analyzing code changes for potential deployment issues."),
    ("user", (
        "Analyze these code changes and determine if they might cause deployment issues:\n\n"
        "Commit SHA: {commit_sha}\n"
        "Commit Message: {commit_message}\n"
        "Files Changed:\n{files_changed}\n\n"
        "Deployment Logs:\n{logs}\n\n"
        "Based on the changes and logs above, are there any potential deployment issues? "
        "Respond with only 'Yes' or 'No'."
    )),
])
deployment_chain = deployment_prompt | llm | StrOutputParser()

fix_commit_prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a DevOps expert. Your job is to fix broken code changes based on Docker logs and commit details."),
    ("user", (
        "The following commit caused a deployment failure. "
        "Here are the details:\n\n"
        "Commit SHA: {commit_sha}\n"
        "Commit Message: {commit_message}\n"
        "Files Changed:\n{files_changed}\n\n"
        "Docker Logs:\n{logs}\n\n"
        "Please provide a fixed version of the commit. "
        "Output the corrected code changes as a unified diff, or describe the necessary changes if a diff is not possible."
    )),
])
fix_commit_chain = fix_commit_prompt | llm | StrOutputParser()

def print_gemini_analysis_output(commit_sha: str, commit_message: str, files_changed_str: str, logs: str, result: str) -> None:
    print("\n[INFO] Raw Gemini Analysis:")
    print("Input to Gemini:")
    print(f"Commit SHA: {commit_sha}")
    print(f"Commit Message: {commit_message}")
    print("Files Changed:")
    print(files_changed_str)
    # Print Deployment Logs
    # print("Logs:")
    # print(logs)
    print("\nGemini's Response:")
    print(result)
    print("-" * 50)

def analyze_deployment_with_gemini(commit_sha: str, commit_message: str, commit_details: dict) -> str:
    logs = fetch_deployment_logs()
    try:
        # Format the files changed for the prompt
        files_changed = []
        for file in commit_details["files"]:
            file_info = f"- {file['filename']} ({file['status']})"
            if file['patch']:
                file_info += f"\n  Changes: {file['patch'][:200]}..."  # Truncate long patches
            files_changed.append(file_info)
        
        files_changed_str = "\n".join(files_changed)
        
        # Get analysis from Gemini
        print("About to call Gemini...")
        result = deployment_chain.invoke({
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "files_changed": files_changed_str,
            "logs": logs
        })
        print("Gemini call complete.")

        # TODO: uncomment to debug
        #print_gemini_analysis_output(commit_sha, commit_message, files_changed_str, logs, result)
        return result.strip()
    except Exception as e:
        print(f"[ERROR] Exception during Gemini analysis: {e}")
        return "Error"

def generate_fixed_commit_with_gemini(commit_sha: str, commit_message: str, commit_details: dict) -> str:
    logs = fetch_deployment_logs()
    try:
        files_changed = []
        for file in commit_details["files"]:
            file_info = f"- {file['filename']} ({file['status']})"
            if file['patch']:
                file_info += f"\n  Changes: {file['patch'][:200]}..."  # Truncate long patches
            files_changed.append(file_info)
        files_changed_str = "\n".join(files_changed)

        print("About to call Gemini for commit fix suggestion...")
        result = fix_commit_chain.invoke({
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "files_changed": files_changed_str,
            "logs": logs
        })
        print("Gemini call complete.")
        # TODO: uncomment to debug
        # print("\n[INFO] Gemini's Fix Suggestion:\n", result)
        print("-" * 50)
        return result.strip()
    except Exception as e:
        print(f"[ERROR] Exception during Gemini commit fix: {e}")
        return "Error"

def create_pr_with_gemini_fix(repo_name, base_branch, pr_branch, diff, pr_title, pr_body, github_token):
    """
    Creates a PR on GitHub with the given diff and description.
    For now, this will only create a branch and PR with the diff and description in the PR body.
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    repo_api = f"https://api.github.com/repos/{repo_name}"
    # 1. Get the latest commit SHA of the base branch
    branch_resp = requests.get(f"{repo_api}/git/ref/heads/{base_branch}", headers=headers)
    branch_resp.raise_for_status()
    base_sha = branch_resp.json()["object"]["sha"]
    # 2. Create a new branch
    data = {
        "ref": f"refs/heads/{pr_branch}",
        "sha": base_sha
    }
    create_branch_resp = requests.post(f"{repo_api}/git/refs", headers=headers, json=data)
    if create_branch_resp.status_code not in (201, 422):  # 422 if branch already exists
        raise Exception(f"Failed to create branch: {create_branch_resp.text}")
    # 3. Create a PR (with the diff and description in the body)
    pr_data = {
        "title": pr_title,
        "head": pr_branch,
        "base": base_branch,
        "body": f"{pr_body}\n\n---\n\nProposed diff:\n\n```diff\n{diff}\n```"
    }
    pr_resp = requests.post(f"{repo_api}/pulls", headers=headers, json=pr_data)
    pr_resp.raise_for_status()
    print(f"PR created: {pr_resp.json()['html_url']}")
    return pr_resp.json()["html_url"]

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
        # TODO: uncomment to debug
        #print_commit_details(commit_sha, commit_message, commit_details)
        print("\n[INFO] Generating fixed commit with Gemini...")
        fixed_diff = generate_fixed_commit_with_gemini(commit_sha, commit_message, commit_details)
        print(f"[INFO] STATUS: fixed_diff")
        # TODO: uncomment to debug
        #print(f"[INFO] Gemini's Fixed Commit Suggestion:\n{fixed_diff}")
        return commit_sha 