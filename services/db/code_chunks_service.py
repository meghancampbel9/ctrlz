from typing import List, Dict, Any

from services.core.supabase_client import get_supabase_client # Corrected import

async def store_code_chunks_batch(chunks_data: list[dict]) -> dict:
    """Stores a batch of code chunks in the Supabase table 'code_chunks'."""
    supabase = get_supabase_client()
    if not supabase:
        return {"error": "Supabase client not available."}
    if not chunks_data:
        return {"message": "No chunks data provided to store."}
    try:
        # Example of one chunk in chunks_data:
        # { 
        #   'repo_id': repo_id, 
        #   'file_path': file_path, 
        #   'chunk_content': chunk_text, 
        #   'start_line': start_line, 
        #   'end_line': end_line,
        #   'embedding': embedding_vector, # Stored as vector type in Supabase
        #   'commit_sha': commit_sha,
        #   'file_hash': file_hash
        # }
        response = await supabase.table('code_chunks').insert(chunks_data).execute()
        if response.data:
            return {"data": response.data, "count": len(response.data)}
        elif response.error:
            print(f"[ERROR] Error storing code chunks batch: {response.error}")
            return {"error": response.error.message if hasattr(response.error, 'message') else str(response.error)}
        else:
            # This case might indicate that the insert happened but no data was returned (e.g. if returning="minimal")
            # Or it could be an unexpected response structure.
            print(f"[WARN] Stored code chunks batch, but no data returned in response. Response: {response}")
            return {"message": "Batch stored, but no data returned.", "response_details": str(response)}

    except Exception as e:
        print(f"[ERROR] Exception storing code chunks batch: {e}")
        import traceback
        return {"error": str(e)}

async def delete_chunks_for_file(repo_id: str, file_path: str, commit_sha: str) -> bool:
    """Deletes all chunks associated with a specific file and commit_sha."""
    supabase = get_supabase_client()
    if not supabase:
        print("[ERROR] Supabase client not available for deleting file chunks.")
        return False
    try:
        response = await supabase.table('code_chunks')\
            .delete()\
            .eq('repo_id', repo_id)\
            .eq('file_path', file_path)\
            .eq('commit_sha', commit_sha)\
            .execute()
        
        if response.error:
            print(f"[ERROR] Error deleting chunks for file {file_path} (sha: {commit_sha}) in repo {repo_id}: {response.error}")
            return False
        
        return True
    except Exception as e:
        print(f"[ERROR] Exception deleting chunks for file {file_path} (sha: {commit_sha}) in repo {repo_id}: {e}")
        return False

async def delete_all_chunks_for_repo(repo_id: str) -> bool:
    """Deletes all chunks associated with a repository."""
    supabase = get_supabase_client()
    if not supabase:
        print("[ERROR] Supabase client not available for deleting all repo chunks.")
        return False
    try:
        response = await supabase.table('code_chunks').delete().eq('repo_id', repo_id).execute()
        if response.error:
            print(f"[ERROR] Error deleting all chunks for repo {repo_id}: {response.error}")
            return False
        return True
    except Exception as e:
        print(f"[ERROR] Exception deleting all chunks for repo {repo_id}: {e}")
        return False

async def get_full_file_content_from_chunks(repo_id: str, file_path: str, commit_sha: str) -> str | None:
    """Reconstructs the full file content by fetching and ordering all its chunks for a specific commit_sha."""
    supabase = get_supabase_client()
    if not supabase:
        print("[ERROR] Supabase client not available for getting full file content.")
        return None
    try:
        # Fetch chunks, ordered by their start_line to ensure correct reconstruction
        response = await supabase.table('code_chunks')\
            .select('chunk_content, start_line, end_line')\
            .eq('repo_id', repo_id)\
            .eq('file_path', file_path)\
            .eq('commit_sha', commit_sha)\
            .order('start_line', desc=False)\
            .execute()

        if response.error:
            print(f"[ERROR] Error fetching chunks to reconstruct file {file_path} (sha: {commit_sha}) for repo {repo_id}: {response.error}")
            return None
        
        if not response.data:
            print(f"[INFO] No chunks found for file {file_path} (sha: {commit_sha}) in repo {repo_id}. Cannot reconstruct content.")
            return None # Or empty string "" if that's more appropriate for your use case

        # Basic reconstruction: concatenate chunk_content. 
        # This assumes non-overlapping, contiguous chunks or that overlap is handled by how chunks were created.
        full_content = "".join([chunk['chunk_content'] for chunk in response.data])
        return full_content

    except Exception as e:
        print(f"[ERROR] Exception reconstructing file content for {file_path} (sha: {commit_sha}) in repo {repo_id}: {e}")
        return None