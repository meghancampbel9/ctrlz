import asyncio
import httpx
import os
import paramiko
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from commit_utils import fix_last_commit

load_dotenv()

EC2_HOST = os.getenv("EC2_HOST")
EC2_USERNAME = os.getenv("EC2_USERNAME")
EC2_KEY_PATH = os.getenv("EC2_KEY_PATH")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

APPS_TO_MONITOR = [
    {"name": "myapp", "url": f"http://{EC2_HOST}/health", "rollback_cmd": "docker stop myapp && docker rm myapp && docker run -d --name myapp myapp:stable", "repo_name": "a-juchacz/flask-gitops-ec2-deploy"}
]

llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash-latest", temperature=0.2)
prompt_template = ChatPromptTemplate.from_messages([
    ("system", "You are a DevOps expert helping to analyze Docker container crash logs."),
    ("user", (
        "My app failed. Here are the logs:\n\n{log_content}\n\n"
        "Based strictly on the logs above, can you tell me if the failure was caused by an issue in the application code? "
        "Respond with only 'Yes' or 'No'."
    )),
])
chain = prompt_template | llm | StrOutputParser()

async def poll_loop():
    print("hello")
    fix_last_commit(APPS_TO_MONITOR[0])
    '''
    while True:
        for app in APPS_TO_MONITOR:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(app["url"])
                    if r.status_code != 200:
                        print(f"[!] Health check failed for {app['name']}, fetching logs...")
                        fetch_docker_logs(app)
                    else:
                        print(f"[OK] {app['name']} is healthy")
            except Exception as e:
                fetch_docker_logs(app)
        await asyncio.sleep(5)
    '''

def fetch_docker_logs(app):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=EC2_HOST,
        username=EC2_USERNAME,
        key_filename=EC2_KEY_PATH
    )
    cmd = f"docker logs {app['name']}"
    stdin, stdout, stderr = ssh.exec_command(cmd)
    logs = stdout.read().decode()
    error = stderr.read().decode()
    ssh.close()

    print("2")
    decision = analyze_logs_with_gemini(logs)
    print(f"DECISION: {decision}")
    print(f"[Gemini Decision] Code issue in {app['name']}: {decision}")

    if decision.lower() == "yes":
        print(f"[!] Triggering rollback for {app['name']}")
        rollback_app(app)
        # TODO: uncomment
        #fix_last_commit(app)
    else:
        print(f"[OK] No rollback needed for {app['name']}")

    return logs

def rollback_app(app):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=EC2_HOST,
        username=EC2_USERNAME,
        key_filename=EC2_KEY_PATH
    )

    rollback_cmd = (
        f"docker stop {app['name']} || true && "
        f"docker rm {app['name']} || true && "
        f"docker run -d --name {app['name']} -p 80:80 {app['name']}:stable"
    )

    ssh.exec_command(rollback_cmd)
    ssh.close()

def analyze_logs_with_gemini(logs: str) -> str:
    result = chain.invoke({"log_content": logs})
    return result.strip()

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
