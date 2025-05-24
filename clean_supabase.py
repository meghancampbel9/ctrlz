import os
import asyncio
from dotenv import load_dotenv
from supabase import create_client, Client

async def clear_all_test_data():
    """Deletes all data from code_chunks and indexed_repositories tables."""
    load_dotenv()

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")

    if not supabase_url or not supabase_key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in your .env file.")
        return

    print(f"Connecting to Supabase at {supabase_url}...")
    supabase: Client = create_client(supabase_url, supabase_key)
    print("Connected to Supabase.")

    try:
        # Delete all rows from code_chunks
        print("Deleting all rows from 'code_chunks' table...")
        # The .delete() method without a filter can be dangerous if not intended.
        # For supabase-py, to delete all, you might need a condition that's always true
        # or check if there's a specific "delete all" method.
        # A common safe way is to select all IDs and then delete by ID,
        # but for a full cleanup, deleting with a wide filter is okay if intended.
        # Supabase often requires some filter for delete, e.g., not null on a primary key.
        # Let's try deleting where primary key is not null (effectively all)
        
        # Simpler approach for full table clear (if allowed and no complex RLS blocks it for service key):
        # This might require a different syntax or be a PostgREST limitation for broad deletes without filters.
        # The safest is to iterate or use a known 'true' condition.
        # However, for `supabase-py` a common pattern is to provide a filter.
        # If we want to delete all, we can use a filter that matches all records, e.g. primary key is not null.
        # For `code_chunks` table (assuming 'id' is UUID primary key):
        response_chunks, count_chunks = supabase.table("code_chunks").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute() # Deletes where id is not a dummy UUID
        
        if response_chunks and hasattr(response_chunks, 'error') and response_chunks.error:
            print(f"Error deleting from code_chunks: {response_chunks.error}")
        else:
            # count_chunks might not be reliable for delete all depending on version/method.
            # Supabase `execute()` for delete typically returns a list of the deleted items in data[1]
            num_deleted_chunks = len(response_chunks[1]) if response_chunks and len(response_chunks) > 1 else "unknown (check Supabase)"
            print(f"Successfully deleted {num_deleted_chunks} rows from 'code_chunks'.")


        # Delete all rows from indexed_repositories
        print("Deleting all rows from 'indexed_repositories' table...")
        # For `indexed_repositories` table (assuming 'repo_id' is TEXT primary key):
        response_repos, count_repos = supabase.table("indexed_repositories").delete().neq("repo_id", "this_is_a_dummy_repo_id_that_will_not_exist").execute() # Deletes where repo_id is not a dummy value

        if response_repos and hasattr(response_repos, 'error') and response_repos.error:
            print(f"Error deleting from indexed_repositories: {response_repos.error}")
        else:
            num_deleted_repos = len(response_repos[1]) if response_repos and len(response_repos) > 1 else "unknown (check Supabase)"
            print(f"Successfully deleted {num_deleted_repos} rows from 'indexed_repositories'.")

        print("Cleanup script finished.")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(clear_all_test_data())