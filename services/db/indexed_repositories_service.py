import time
from typing import Dict
from ..core.supabase_client import get_supabase_client

async def update_repository_indexing_status(repo_id: str, owner: str, repo_name: str, status: str, commit_sha: str = None, file_hashes: Dict = None, error_message: str = None):
    """Updates the indexing status of a repository in the 'indexed_repositories' table."""
    client = get_supabase_client()
    if not client:
        return {"error": "Supabase client not available"}

    record = {
        "owner": owner,
        "repo_name": repo_name,
        "status": status,
        "last_indexed_at": time.strftime('%Y-%m-%d %H:%M:%S %Z', time.gmtime()) # UTC
    }
    if commit_sha:
        record["last_indexed_commit_sha"] = commit_sha
    if file_hashes:
        record["file_hashes"] = file_hashes
    if error_message:
        record["error_message"] = error_message
    
    try:
        data, count = client.table("indexed_repositories").upsert({"repo_id": repo_id, **record}).execute()
        return {"data": data, "count": count}
    except Exception as e:
        print(f"Error updating repository status for {repo_id}: {e}")
        return {"error": str(e)}

async def check_if_repo_indexed(repo_id: str) -> Dict | None:
    """Checks if a repository is already indexed and its last commit SHA."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        response = client.table("indexed_repositories").select("last_indexed_commit_sha", "file_hashes", "status").eq("repo_id", repo_id).maybe_single().execute()
        if response.data:
            return response.data
        return None
    except Exception as e:
        print(f"Error checking indexed status for {repo_id}: {e}")
        return None

async def get_indexed_file_hashes(repo_id: str) -> Dict[str, str] | None:
    """Retrieves the stored file hashes for a repository."""
    client = get_supabase_client()
    if not client:
        return None
    try:
        response = client.table("indexed_repositories").select("file_hashes").eq("repo_id", repo_id).maybe_single().execute()
        if response.data and response.data.get("file_hashes"):
            return response.data["file_hashes"]
        return {} # Return empty dict if no hashes found or no record
    except Exception as e:
        print(f"Error getting file hashes for {repo_id}: {e}")
        return None 