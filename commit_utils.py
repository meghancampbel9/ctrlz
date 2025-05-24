import httpx
import os
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
        
        # Print the raw output from Gemini
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
        
        return result.strip()
    except Exception as e:
        print(f"[ERROR] Exception during Gemini analysis: {e}")
        return "Error"

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
        
        # Analyze deployment with Gemini
        print("\n[INFO] Analyzing deployment with Gemini...")
        deployment_analysis = analyze_deployment_with_gemini(commit_sha, commit_message, commit_details)
        print(f"[INFO] Deployment Analysis: {deployment_analysis}")
        
        return commit_sha
    return None 