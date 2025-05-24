import asyncio
import httpx
import os
import paramiko
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
import subprocess
import os
import datetime

load_dotenv()

EC2_HOST = os.getenv("EC2_HOST")
EC2_USERNAME = os.getenv("EC2_USERNAME")
EC2_KEY_PATH = os.getenv("EC2_KEY_PATH")
LATEST_IMAGE = os.getenv("LATEST_IMAGE")
STABLE_IMAGE = os.getenv("STABLE_IMAGE")

APPS_TO_MONITOR = [
    {
        "name": "myapp",
        "url": f"http://{EC2_HOST}/health"
    }
]

llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash-latest", temperature=0.2)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a DevOps engineer reviewing failed deployments. Use the health check response, Docker logs, and app source code to generate a Git-ready code patch."),
    ("user", (
        "Health check response:\n\n{health_response}\n\n"
        "Docker logs:\n\n{docker_logs}\n\n"
        "Full source code of the app:\n\n{code_text}\n\n"
        "Please provide a unified diff (git-style patch) that fixes the bug and improves health check reliability. Only include the diff."
    ))
])


chain = prompt | llm | StrOutputParser()

def read_file_contents(filepath: str) -> str:
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except Exception as e:
        return f"ERROR reading file: {e}"

async def analyze_failure(health_response: str, logs: str, code_text: str) -> str:
    result = await chain.ainvoke({
        "health_response": health_response,
        "docker_logs": logs,
        "code_text": code_text
    })
    return result.strip()

async def fetch_docker_logs_and_status(app):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    _, stdout, _ = ssh.exec_command(f"docker inspect -f '{{{{.State.Status}}}}' {app['name']}")
    status = stdout.read().decode().strip()
    print(f"[INFO] Status for {app['name']}: {status}")

    if status == "exited":
        _, stdout, _ = ssh.exec_command(f"docker inspect -f '{{{{.State.ExitCode}}}}' {app['name']}")
        exit_code = int(stdout.read().decode().strip())
    else:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(app["url"])
                if r.status_code != 200:
                    print(f"[INFO] App is running but unhealthy (status {r.status_code})")
                    exit_code = 0
                else:
                    exit_code = 200
        except Exception as e:
            print(f"[ERROR] Health check failed while running: {e}")
            exit_code = 0

    _, stdout, _ = ssh.exec_command(f"docker logs {app['name']}")
    logs = stdout.read().decode()

    ssh.close()
    return status, exit_code, logs

async def restart_latest(app):
    print(f"[ACTION] Restarting latest image for {app['name']} with docker run command...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    restart_cmd = f"""
      docker stop {app['name']} || true
      docker rm {app['name']} || true
      docker pull {LATEST_IMAGE} || true
      docker run -d --name {app['name']} -p 80:80 {LATEST_IMAGE}
    """
    ssh.exec_command(restart_cmd)
    print(f"[INFO] Restart command executed for {app['name']}.")
    ssh.close()

async def restart_stable(app):
    print(f"[ACTION] Restarting stable image for {app['name']}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    restart_cmd = (
        f"docker stop {app['name']} || true && " #Fixed typo here
        f"docker rm {app['name']} || true && "
        f"docker run -d --name {app['name']} -p 80:80 {STABLE_IMAGE}"
    )
    ssh.exec_command(restart_cmd)
    ssh.close()
    print(f"[PAUSE] Waiting 30 seconds after stable restart for {app['name']}...")
    await asyncio.sleep(30)

async def get_health_response(app) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(app["url"])
            return f"{r.status_code} {r.text}"
    except Exception as e:
        return f"ERROR: {e}"

async def poll_loop():
    while True:
        for app in APPS_TO_MONITOR:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(app["url"], timeout=5)
                    if r.status_code != 200:
                        print(f"[!] Health check failed for {app['name']} (HTTP {r.status_code}), analyzing...")
                        status, exit_code, logs = await fetch_docker_logs_and_status(app)

                        if exit_code in (0, 1):
                            #await restart_stable(app)
                            health_response = await get_health_response(app)
                            code_text = read_file_contents("bot.py")  # adjust path if needed
                            diagnosis = await analyze_failure(health_response, logs, code_text)
                            with open("suggested_fix.patch", "w") as f:
                                f.write(diagnosis)

                            branch = apply_patch_and_push_branch()
                            if branch:
                                print(f"[NEXT] Open a PR from `{branch}` to `main`")


                            print("[INFO] Saved suggested fix to suggested_fix.patch")
                            print(f"[Gemini Diagnosis]:\n{diagnosis}")
                        elif exit_code == 137:
                            await restart_latest(app)
                        elif exit_code == 200:
                            print(f"[OK] {app['name']} appears healthy.")
                        else:
                            print(f"[WARN] Unknown state for {app['name']}. Restarting as precaution.")
                            await restart_latest(app)
                    else: 
                        print(f"[OK] {app['name']} is healthy")
            except Exception as e:
                print(f"[ERROR] Exception occurred during health check: {e}. Checking container state...")
                status, exit_code, logs = await fetch_docker_logs_and_status(app)
                if exit_code in (0, 1):
                    #await restart_stable(app)
                    health_response = await get_health_response(app)
                    code_text = read_file_contents("bot.py")
                    diagnosis = await analyze_failure(health_response, logs, code_text)

                    with open("suggested_fix.patch", "w") as f:
                        f.write(diagnosis)

                    branch = apply_patch_and_push_branch()
                    if branch:
                        print(f"[NEXT] Open a PR from `{branch}` to `main`")

                    print("[INFO] Saved suggested fix to suggested_fix.patch")
                    print(f"[Gemini Diagnosis]:\n{diagnosis}")
                elif exit_code == 137:
                    await restart_latest(app)
        await asyncio.sleep(30) #Increased sleep time to allow for container restart and health check

def apply_patch_and_push_branch(patch_path="suggested_fix.patch", repo_path="."):
    branch_name = f"auto-fix-health-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
    print(f"[INFO] Creating branch: {branch_name}")

    try:
        subprocess.run(["git", "apply", patch_path], check=True, cwd=repo_path)
        print("[INFO] Patch applied successfully.")

        subprocess.run(["git", "checkout", "-b", branch_name], check=True, cwd=repo_path)

        subprocess.run(["git", "add", "."], check=True, cwd=repo_path)
        subprocess.run(["git", "commit", "-m", "Auto-generated health check fix"], check=True, cwd=repo_path)
        subprocess.run(["git", "push", "-u", "origin", branch_name], check=True, cwd=repo_path)

        print(f"[SUCCESS] Patch pushed to new branch: {branch_name}")
        return branch_name
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Git operation failed: {e}")
        return None


if __name__ == "__main__":
    asyncio.run(poll_loop())
