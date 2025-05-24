import asyncio
import httpx
import openai
from dotenv import load_dotenv
import os
import paramiko

load_dotenv()

EC2_HOST = os.getenv("EC2_HOST")
EC2_USERNAME = os.getenv("EC2_USERNAME")
EC2_KEY_PATH = os.getenv("EC2_KEY_PATH")

APPS_TO_MONITOR = [
    {"name": "myapp", "url": f"{EC2_HOST}/health", "rollback_cmd": "docker start myapp-good"}
]
openai.api_key = os.getenv("OPENAI_API_KEY")

async def poll_loop():
    while True:
        for app in APPS_TO_MONITOR:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(app["url"])
                    if r.status_code != 200:
                        print("Application not healthy, fetching docker logs")
                        fetch_docker_logs()
                    else:
                        print("Application healthy")
                        print(fetch_docker_logs())
            except Exception as e:
                print(f"[!] Error checking {app['name']}: {e}")
        await asyncio.sleep(5)


def fetch_docker_logs() -> str:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=EC2_HOST,
        username=EC2_USERNAME,
        key_filename=EC2_KEY_PATH
    )

    cmd = f"docker logs myapp"
    stdin, stdout, stderr = ssh.exec_command(cmd)
    logs = stdout.read().decode()
    error = stderr.read().decode()

    ssh.close()

    if error and not logs:
        raise RuntimeError(f"Failed to fetch logs: {error}")
    else:
        print(analyze_logs_with_gpt(logs))

    return logs


def rollback_app(app):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("3.76.115.47", username="ubuntu", key_filename="ubuntu.pem")
    ssh.exec_command(app["rollback_cmd"])
    ssh.close()

def analyze_logs_with_gpt(logs: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are a DevOps expert helping to analyze Docker container crash logs."},
            {"role": "user", "content": f"My app failed. Here are the logs:\n\n{logs}"}
        ],
        max_tokens=300
    )
    return response.choices[0].message.content.strip()