import asyncio
import httpx
import paramiko
from dotenv import load_dotenv
import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

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

llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash-latest", temperature=0.3)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You're a DevOps assistant diagnosing why a Dockerized app failed."),
    ("user", (
        "Docker logs:\n\n{docker_logs}\n\n"
        "Health check response:\n\n{health_response}\n\n"
        "Please explain the most likely cause of failure in 1-2 sentences. "
        "Tell me if you think it's an infrastructural or code issue. Answer with CODE or INFRASTRUCTURE"
    ))
])

chain = prompt | llm | StrOutputParser()

async def poll_loop():
    while True:
        for app in APPS_TO_MONITOR:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(app["url"], timeout=5)
                    if r.status_code != 200:
                        print(f"[!] Health check failed for {app['name']} (HTTP {r.status_code}), analyzing...")
                        await check_container_exited_status(app)
                    else:
                        print(f"[OK] {app['name']} is healthy")
            except Exception as e:
                print(f"[ERROR] Exception occurred during health check: {e}. Your container has probably stopped.")
                print(f"Checking container state...")
                await check_container_exited_status(app)
        # Increased sleep time to allow for container restart and health check
        await asyncio.sleep(30)

async def get_health_response(app) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(app["url"])
            return f"{r.status_code} {r.text}"
    except Exception as e:
        return f"ERROR: {e} - Check if the application is running and listening on port 80"

async def check_container_exited_status(app):
    status, exit_code, logs = await fetch_docker_logs_and_status(app)
    # exit codes 0 and 1 indicate issue with the app
    # anything else is infra related
    if exit_code in (0, 1) and status == "exited":
        await restart_stable(app)
        health_response = await get_health_response(app)
        diagnosis = await diagnose_failure(logs, health_response)
        print(f"[AI Diagnosis]: {diagnosis}")
        if(diagnosis == "CODE"):
            print("create pr")
    elif status == "exited" and exit_code not in (0, 1):
        await restart_latest(app)
    else:
        print(f"[WARN] Unknown state for {app['name']}. Restarting as precaution.")
        await restart_latest(app)

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
    print(f"[ACTION] Restarting latest image for {app['name']} (attempting to fix health check failure)...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    restart_cmd = f"""
      docker stop {app['name']} || true
      docker rm {app['name']} || true
      docker pull {LATEST_IMAGE} || true
      docker run -d --name {app['name']} -p 80:80 --restart=always {LATEST_IMAGE} || true
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
        f"docker stop {app['name']} || true && " 
        f"docker rm {app['name']} || true && "
        f"docker run -d --name {app['name']} -p 80:80 {STABLE_IMAGE}"
    )
    ssh.exec_command(restart_cmd)
    ssh.close()
    print(f"[PAUSE] Waiting 30 seconds after stable restart for {app['name']}")
    await asyncio.sleep(30)


async def diagnose_failure(logs: str, health_response: str = None) -> str:
    try:
        result = await chain.ainvoke({
            "docker_logs": logs,
            "health_response": health_response
        })
        return result.strip()
    except Exception as e:
        return f"[DIAGNOSIS ERROR] Failed to analyze logs: {e}"


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


if __name__ == "__main__":
    asyncio.run(poll_loop())
