import time
import jwt
import httpx
from config import APP_ID, PRIVATE_KEY 

if not APP_ID:
    print("[CRITICAL ERROR] APP_ID not configured via config.py for auth.py.")
if not PRIVATE_KEY:
     print("[CRITICAL ERROR] PRIVATE_KEY not loaded via config.py for auth.py.")

def generate_jwt():
    if not APP_ID or not PRIVATE_KEY:
        print("[ERROR] APP_ID or PRIVATE_KEY not available from config for JWT generation.")
        raise ValueError("APP_ID or PRIVATE_KEY not configured for JWT generation.")
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": APP_ID
    }
    try:
        encoded_jwt = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
        return encoded_jwt
    except Exception as e:
        print(f"[ERROR] JWT Encoding failed: {e}")
        raise

async def get_installation_access_token(installation_id: str) -> str | None:
    try:
        jwt_token = generate_jwt()
    except ValueError as e:
        print(f"[ERROR] Cannot get installation access token, JWT generation failed: {e}")
        return None

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, headers=headers)
            resp.raise_for_status()
            token_data = resp.json()
            if "token" not in token_data:
                print(f"[GitHub API Error] 'token' field missing in response from {url}. Response: {token_data}")
                return None
            return token_data["token"]
        except httpx.HTTPStatusError as e:
            print(f"[GitHub API Error] Failed to get installation access token for ID {installation_id}. Status: {e.response.status_code}, Response: {e.response.text}")
            return None
        except Exception as e:
            print(f"[GitHub API Error] Unexpected error getting installation token: {e}")
            return None 