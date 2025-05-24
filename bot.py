import asyncio
import httpx
import os
import paramiko
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

EC2_HOST = os.getenv("EC2_HOST")
EC2_USERNAME = os.getenv("EC2_USERNAME")
EC2_KEY_PATH = os.getenv("EC2_KEY_PATH")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

APPS_TO_MONITOR = [
    {"name": "myapp", "url": f"http://{EC2_HOST}/health", "rollback_cmd": "docker stop myapp && docker rm myapp && docker run -d --name myapp myapp:stable"}
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
