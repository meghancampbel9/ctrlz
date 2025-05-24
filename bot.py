import asyncio
import httpx
import os
import paramiko
from dotenv import load_dotenv

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
    print(f"[ACTION] Restarting latest image for {app['name']}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    restart_cmd = f"""
      docker stop {app['name']} || true
      docker rm {app['name']} || true
      docker pull {LATEST_IMAGE}
      docker run -d --name {app['name']} -p 80:80 {LATEST_IMAGE}
    """
    ssh.exec_command(restart_cmd)
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
    print(f"[PAUSE] Waiting 30 seconds after stable restart...")
    await asyncio.sleep(30)

async def restart_latest(app):
    print(f"[ACTION] Restarting latest image for {app['name']}")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=EC2_HOST, username=EC2_USERNAME, key_filename=EC2_KEY_PATH)

    restart_cmd = f"""
      docker stop {app['name']} || true
      docker rm {app['name']} || true
      docker pull {LATEST_IMAGE}
      docker run -d --name {app['name']} -p 80:80 {LATEST_IMAGE}
    """
    ssh.exec_command(restart_cmd)
    ssh.close()

async def poll_loop():
    while True:
        for app in APPS_TO_MONITOR:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(app["url"])
                    if r.status_code != 200:
                        print(f"[!] Health check failed for {app['name']}, analyzing...")
                        status, exit_code, logs = await fetch_docker_logs_and_status(app)

                        if exit_code in (0, 1):
                            await restart_stable(app)
                        elif exit_code == 137:
                            await restart_latest(app)
                        else:
                            print(f"[WARN] Unknown state for {app['name']}. Restarting as precaution.")
                            await restart_latest(app)
                    else:
                        print(f"[OK] {app['name']} is healthy")
            except Exception as e:
                print(f"[ERROR] Exception occurred: {e}. Checking container state...")
                status, exit_code, logs = await fetch_docker_logs_and_status(app)
                if exit_code in (0, 1):
                    await restart_stable(app)
                elif exit_code == 137:
                    await restart_latest(app)
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(poll_loop())