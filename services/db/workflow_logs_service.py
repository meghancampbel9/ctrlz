from typing import List, Dict, Any
from ..core.supabase_client import get_supabase_client # Adjusted import

async def store_workflow_log(run_id: int, repository_full_name: str, workflow_name: str | None, job_name: str | None, log_filename: str, log_content: str) -> Dict:
    """Stores a single workflow log entry into the 'workflow_logs' table."""
    client = get_supabase_client()
    if not client:
        return {"error": "Supabase client not available for storing workflow log."}

    record = {
        "run_id": run_id,
        "repository_full_name": repository_full_name,
        "workflow_name": workflow_name,
        "job_name": job_name,
        "log_filename": log_filename,
        "log_content": log_content
    }
    try:
        data, count = client.table("workflow_logs").insert(record).execute()
        return {"data": data, "count": count}
    except Exception as e:
        print(f"Error storing workflow log {log_filename} for run {run_id}: {e}")
        return {"error": str(e)}

async def get_workflow_logs_for_run(run_id: int, repository_full_name: str) -> List[Dict[str, Any]]:
    """Retrieves all workflow logs for a specific run_id and repository."""
    client = get_supabase_client()
    if not client:
        print("Supabase client not available for retrieving workflow logs.")
        return []

    try:
        response = (
            client.table("workflow_logs")
            .select("run_id, repository_full_name, workflow_name, job_name, log_filename, log_content, created_at")
            .eq("run_id", run_id)
            .eq("repository_full_name", repository_full_name)
            .order("created_at", desc=False) # Order by creation time to keep logs in sequence
            .execute()
        )
        if response.data:
            return response.data
        else:
            return []
    except Exception as e:
        print(f"Error retrieving workflow logs for run {run_id}, repo {repository_full_name}: {e}")
        return [] 