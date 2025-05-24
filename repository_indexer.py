import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Dict, Tuple
import git # GitPython
import asyncio

from supabase_service import (
    update_repository_indexing_status,
    store_code_chunks_batch,
    chunk_file_content,
    embed_chunks,
    calculate_file_hash,
    get_indexed_file_hashes,
    delete_chunks_for_file,
    # delete_all_chunks_for_repo # Might be useful for full re-index
)

# Define file extensions to include for indexing (can be expanded)
# Using a set for efficient lookup
RELEVANT_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go",
    ".rb", ".php", ".swift", ".kt", ".scala", ".rs", ".lua", ".pl", ".pm",
    ".sh", ".bash", ".zsh", ".fish",
    ".md", ".txt", ".json", ".yaml", ".yml", ".xml", ".html", ".css", ".scss",
    ".dockerfile", "Dockerfile", ".tf", ".hcl"
    # Add more as needed, consider common config files too
}

# Define directories and specific files/patterns to always ignore
# Using a set for efficient lookup
IGNORE_PATTERNS = {
    ".git", ".hg", ".svn",                           # VCS directories
    "__pycache__", ".pytest_cache", ".mypy_cache",   # Python cache
    "node_modules", "bower_components",               # JS dependencies
    "vendor", "third_party",                         # Common dependency dirs
    ".DS_Store", "Thumbs.db",                        # OS-specific files
    "*.min.js", "*.min.css",                         # Minified files
    "*.pyc", "*.pyo", "*.pyd",                      # Python compiled/optimized
    "*.o", "*.so", "*.dll", "*.exe", "*.bin",        # Compiled objects/binaries
    "*.jar", "*.war", "*.ear",                      # Java archives
    "*.zip", "*.tar.gz", "*.rar", "*.7z",             # Archives
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.ico", "*.svg", # Images
    "*.mp3", "*.wav", "*.ogg", "*.mp4", "*.avi", "*.mov", "*.webm", # Media files
    ".env", "*.lock",                                # Environment files, lock files
    "package-lock.json", "yarn.lock", "composer.lock", "Pipfile.lock", "poetry.lock"
}

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB limit for individual files

def should_index_file(file_path: Path) -> bool:
    """Determines if a file should be indexed based on extension, patterns, and size."""
    if not file_path.is_file():
        return False

    # 1. Check file size
    try:
        file_stat = file_path.stat()
        if file_stat.st_size > MAX_FILE_SIZE_BYTES:
            print(f"Skipping {file_path} due to size ({file_stat.st_size} bytes).")
            return False
        if file_stat.st_size == 0:
            print(f"Skipping empty file {file_path}.")
            return False
    except OSError as e:
        print(f"Error stating file {file_path}: {e}. Skipping.")
        return False

    # 2. Check against IGNORE_PATTERNS
    # Check if any part of the path is an exact match in IGNORE_PATTERNS
    if any(part in IGNORE_PATTERNS for part in file_path.parts):
        # print(f"Skipping {file_path} due to directory part ignore pattern.") # Can be noisy
        return False
    # Check if the file name itself is an exact match in IGNORE_PATTERNS
    if file_path.name in IGNORE_PATTERNS:
        # print(f"Skipping {file_path} due to file name ignore pattern.") # Can be noisy
        return False
    # Check for glob-style patterns in IGNORE_PATTERNS
    if any(file_path.match(pattern) for pattern in IGNORE_PATTERNS if "*" in pattern or "?" in pattern):
        # print(f"Skipping {file_path} due to glob ignore pattern.") # Can be noisy
        return False

    # 3. Check for relevant extensions or exact file names
    if file_path.suffix.lower() in RELEVANT_EXTENSIONS or file_path.name in RELEVANT_EXTENSIONS:
        return True
    
    # print(f"Skipping {file_path} due to non-relevant extension/name.") # Can be noisy
    return False


async def process_repository(repo_url: str, repo_id: str, owner: str, repo_name: str, branch: str = "main", installation_token: str = None):
    """
    Clones a repository, processes its files, generates embeddings, and stores them.
    Handles initial indexing and re-indexing based on file hashes.
    Args:
        repo_url (str): The HTTPS URL to clone the repository (e.g., https://github.com/owner/repo.git)
        repo_id (str): Unique identifier for the repository (e.g., owner/repo_name or GitHub numeric ID)
        owner (str): Repository owner login.
        repo_name (str): Repository name.
        branch (str): The branch to clone and index.
        installation_token (str, optional): GitHub App installation token for private repos.
    """
    temp_dir_path = None # Initialize to ensure it's defined in finally block
    current_commit_sha = None
    processed_files_hashes = {}

    try:
        # 0. Mark repository as 'indexing'
        print(f"Starting indexing for {repo_id}...")
        await update_repository_indexing_status(repo_id, owner, repo_name, "indexing")

        # 1. Create a temporary directory for cloning
        temp_dir_path = Path(tempfile.mkdtemp(prefix="repo_indexer_"))
        print(f"Cloning {repo_url} (branch: {branch}) into {temp_dir_path}")

        # Modify clone URL if token is provided (for private repos)
        # This is a common pattern; ensure your Git version supports this.
        if installation_token:
            clone_url = repo_url.replace("https://", f"https://x-access-token:{installation_token}@")
        else:
            clone_url = repo_url

        # 2. Clone the repository
        try:
            cloned_repo = await asyncio.to_thread(
                git.Repo.clone_from, clone_url, temp_dir_path, branch=branch, depth=1 # Shallow clone for latest commit
            )
            current_commit_sha = str(cloned_repo.head.commit.hexsha)
            print(f"Successfully cloned {repo_id}. Current commit SHA: {current_commit_sha}")
        except git.GitCommandError as e:
            print(f"Git clone failed for {repo_id}: {e}")
            await update_repository_indexing_status(repo_id, owner, repo_name, "failed", error_message=f"Git clone error: {e}")
            return
        
        # 3. Get previously indexed file hashes (if any) for comparison
        existing_file_hashes = await get_indexed_file_hashes(repo_id) or {}
        print(f"Found {len(existing_file_hashes)} existing file hashes for {repo_id}.")

        # 4. Iterate through files, process, and store
        files_to_process_tasks = []
        all_repo_file_paths = list(temp_dir_path.rglob("*")) # Get all files recursively
        
        print(f"Scanning {len(all_repo_file_paths)} total paths in {repo_id}...")

        files_processed_count = 0
        files_skipped_count = 0
        # Start with all previously known files as candidates for deletion if not found again
        files_potentially_deleted_or_skipped = set(existing_file_hashes.keys())

        for file_abs_path in all_repo_file_paths:
            if not file_abs_path.is_file():
                continue

            relative_file_path_str = str(file_abs_path.relative_to(temp_dir_path))
            
            # If found, it's not deleted, so remove from this set
            files_potentially_deleted_or_skipped.discard(relative_file_path_str)

            if not should_index_file(file_abs_path):
                files_skipped_count += 1
                # If a file was indexed before but now shouldn't be (e.g. added to IGNORE_PATTERNS or too big)
                if relative_file_path_str in existing_file_hashes:
                    print(f"File {relative_file_path_str} in {repo_id} was indexed but is now skipped. Deleting old chunks.")
                    await delete_chunks_for_file(repo_id, relative_file_path_str)
                    # Remove from existing_file_hashes so we don't carry over its old hash
                    if relative_file_path_str in processed_files_hashes: # Should be empty at this stage of loop for this file
                        del processed_files_hashes[relative_file_path_str]
                    if relative_file_path_str in existing_file_hashes:
                         del existing_file_hashes[relative_file_path_str]
                continue
            
            try:
                file_content_bytes = await asyncio.to_thread(file_abs_path.read_bytes)
                file_content_str = file_content_bytes.decode('utf-8', errors='replace')
            except Exception as e:
                print(f"Error reading file {relative_file_path_str} in {repo_id}: {e}. Skipping.")
                files_skipped_count += 1
                continue

            current_file_hash = calculate_file_hash(file_content_str)
            
            if existing_file_hashes.get(relative_file_path_str) == current_file_hash:
                # print(f"File {relative_file_path_str} in {repo_id} is unchanged. Skipping re-embedding.") # Can be noisy
                processed_files_hashes[relative_file_path_str] = current_file_hash # Still record its hash as processed
                files_skipped_count += 1
                continue # File content hasn't changed, no need to re-embed
            else:
                if relative_file_path_str in existing_file_hashes:
                    print(f"File {relative_file_path_str} in {repo_id} has changed. Re-indexing.")
                    await delete_chunks_for_file(repo_id, relative_file_path_str) # Delete old chunks before adding new
                else:
                    print(f"New file {relative_file_path_str} in {repo_id}. Indexing.")

                processed_files_hashes[relative_file_path_str] = current_file_hash # Record new hash
                # Create a task for processing this file
                files_to_process_tasks.append(
                    process_single_file(repo_id, current_commit_sha, relative_file_path_str, file_content_str)
                )
                files_processed_count += 1

        # Process files in parallel (or concurrently)
        if files_to_process_tasks:
            print(f"Processing {len(files_to_process_tasks)} new/changed files for {repo_id}...")
            await asyncio.gather(*files_to_process_tasks)
        
        # 5. Delete chunks for files that are no longer in the repo (were in files_potentially_deleted_or_skipped)
        for file_path_to_delete in files_potentially_deleted_or_skipped:
            print(f"File {file_path_to_delete} in {repo_id} seems to be removed. Deleting its chunks.")
            await delete_chunks_for_file(repo_id, file_path_to_delete)
            # No need to touch processed_files_hashes here, as these files were not processed in this run

        # 6. Mark repository as 'completed' with the new commit SHA and file hashes
        await update_repository_indexing_status(repo_id, owner, repo_name, "completed", current_commit_sha, processed_files_hashes)
        print(f"Successfully completed indexing for {repo_id} at commit {current_commit_sha}.")
        print(f"Files processed/re-indexed: {files_processed_count}, Files skipped/unchanged: {files_skipped_count}")

    except Exception as e:
        print(f"An unexpected error occurred during indexing of {repo_id}: {e}")
        # Ensure these are available for status update
        final_owner = owner if 'owner' in locals() else "unknown_owner"
        final_repo_name = repo_name if 'repo_name' in locals() else "unknown_repo"
        final_repo_id = repo_id if 'repo_id' in locals() else f"{final_owner}/{final_repo_name}"
        
        await update_repository_indexing_status(
            final_repo_id, 
            final_owner, 
            final_repo_name, 
            "failed", 
            commit_sha=current_commit_sha, 
            error_message=str(e)
        )
    finally:
        # 7. Clean up the temporary directory
        if temp_dir_path and temp_dir_path.exists():
            try:
                shutil.rmtree(temp_dir_path)
                print(f"Successfully removed temporary directory {temp_dir_path}")
            except Exception as e:
                print(f"Error removing temporary directory {temp_dir_path}: {e}")

async def process_single_file(repo_id: str, commit_sha: str, relative_file_path: str, file_content: str):
    """Chunks, embeds, and stores a single file's content."""
    try:
        # 1. Chunk content
        langchain_docs = chunk_file_content(file_content, relative_file_path)
        if not langchain_docs:
            print(f"No chunks generated for {relative_file_path} in {repo_id}. Skipping.")
            return

        # 2. Embed chunks
        chunk_embeddings = await embed_chunks(langchain_docs)
        if not chunk_embeddings or len(chunk_embeddings) != len(langchain_docs):
            print(f"Embedding failed or mismatch for {relative_file_path} in {repo_id}. Expected {len(langchain_docs)}, got {len(chunk_embeddings) if chunk_embeddings else 0}.")
            return
        
        # 3. Prepare for batch storage
        chunks_to_store = [
            {"chunk_content": doc.page_content, "embedding": emb}
            for doc, emb in zip(langchain_docs, chunk_embeddings)
        ]

        # 4. Store chunks in Supabase
        await store_code_chunks_batch(repo_id, commit_sha, relative_file_path, chunks_to_store)
        # print(f"Successfully processed and stored file: {relative_file_path} in {repo_id}") # Can be noisy

    except Exception as e:
        print(f"Error processing file {relative_file_path} in {repo_id}: {e}")
        # Optionally, update a per-file status if needed, or let the main error handler catch it.

# --- Example Usage (for testing this module directly) ---
if __name__ == '__main__':
    # Ensure you have a .env file with SUPABASE_URL, SUPABASE_SERVICE_KEY, GOOGLE_API_KEY
    # And a test repository URL (public for no token, or set up a token for private)
    TEST_REPO_URL = "https://github.com/langchain-ai/langchain.git" # A large public repo
    TEST_REPO_ID = "langchain-ai/langchain_test_indexer"
    TEST_OWNER = "langchain-ai"
    TEST_REPO_NAME = "langchain_test_indexer"
    TEST_BRANCH = "master" # or "main"

    # To test with a private repo, you'd need a valid installation_token for that repo
    # TEST_PRIVATE_REPO_URL = "https://github.com/your_org/your_private_repo.git"
    # TEST_PRIVATE_REPO_ID = "your_org/your_private_repo"
    # TEST_PRIVATE_OWNER = "your_org"
    # TEST_PRIVATE_REPO_NAME = "your_private_repo"
    # INSTALLATION_TOKEN = os.getenv("YOUR_GITHUB_INSTALLATION_TOKEN") # Get this dynamically in app.py

    async def run_test_indexing():
        print("Starting test indexing...")
        # Before running, you might want to clean up previous test data in Supabase for this TEST_REPO_ID
        from supabase_service import get_supabase_client, delete_all_chunks_for_repo, supabase # Import supabase client directly for this
        # supabase_client = get_supabase_client()
        if supabase: # Check if supabase client from supabase_service is available
            print(f"Cleaning up old chunks for {TEST_REPO_ID}...")
            await delete_all_chunks_for_repo(TEST_REPO_ID) # This is an async function from supabase_service
            print(f"Cleaning up old repository record for {TEST_REPO_ID}...")
            # The direct supabase client call is synchronous
            supabase.table("indexed_repositories").delete().eq("repo_id", TEST_REPO_ID).execute()
        else:
            print("Supabase client not available for cleanup.")
            # return # Optionally return if cleanup is critical before test

        await process_repository(
            repo_url=TEST_REPO_URL, 
            repo_id=TEST_REPO_ID, 
            owner=TEST_OWNER, 
            repo_name=TEST_REPO_NAME, 
            branch=TEST_BRANCH
        )
        print(f"Test indexing finished for {TEST_REPO_ID}.")

    asyncio.run(run_test_indexing()) 