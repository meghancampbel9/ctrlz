import os
from supabase import create_client, Client
from dotenv import load_dotenv
from typing import List, Dict, Any
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
from langchain_core.documents import Document
import hashlib
import time

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Ensure this is set for embeddings

# Initialize Supabase client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
except Exception as e:
    print(f"Error initializing Supabase client: {e}")
    supabase = None

# Initialize Embeddings model
try:
    embeddings_model = GoogleGenerativeAIEmbeddings(model="models/embedding-001", google_api_key=GOOGLE_API_KEY)
    EMBEDDING_DIMENSION = 768 # As specified for models/embedding-001
except Exception as e:
    print(f"Error initializing GoogleGenerativeAIEmbeddings: {e}")
    print("Ensure GOOGLE_API_KEY is set and valid.")
    embeddings_model = None

def get_supabase_client() -> Client | None:
    if supabase is None:
        print("Supabase client not initialized. Check SUPABASE_URL and SUPABASE_SERVICE_KEY.")
    return supabase

def get_embeddings_model():
    if embeddings_model is None:
        print("Embeddings model not initialized. Check GOOGLE_API_KEY.")
    return embeddings_model

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

async def store_code_chunks_batch(repo_id: str, commit_sha: str, file_path: str, chunks_with_embeddings: List[Dict[str, Any]]):
    """Stores a batch of code chunks with their embeddings into the 'code_chunks' table."""
    client = get_supabase_client()
    if not client:
        return {"error": "Supabase client not available"}

    records_to_insert = [
        {
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "file_path": file_path,
            "chunk_content": item["chunk_content"],
            "embedding": item["embedding"]
        } for item in chunks_with_embeddings
    ]

    if not records_to_insert:
        return {"data": [], "count": 0, "message": "No records to insert."}

    try:
        data, count = client.table("code_chunks").insert(records_to_insert).execute()
        print(f"Successfully inserted {len(data[1]) if data and len(data)>1 else 0} chunks for {file_path} in {repo_id}")
        return {"data": data, "count": count}
    except Exception as e:
        print(f"Error inserting chunks for {file_path} in {repo_id}: {e}")
        return {"error": str(e)}

def chunk_file_content(file_content: str, file_path: str, chunk_size=1000, chunk_overlap=200) -> List[Document]:
    """Chunks file content based on its type. Returns list of Langchain Documents."""
    file_extension = file_path.split('.')[-1].lower()
    
    # Map file extensions to Langchain Language enum if possible
    # This helps RecursiveCharacterTextSplitter do a better job for supported languages
    lang_map = {
        "py": Language.PYTHON,
        "js": Language.JS,
        "ts": Language.TS,
        "md": Language.MARKDOWN,
        "java": Language.JAVA,
        "c": Language.C,
        "cpp": Language.CPP,
        "cs": Language.CSHARP,
        "go": Language.GO,
        "html": Language.HTML,
        "php": Language.PHP,
        "rb": Language.RUBY,
        "rs": Language.RUST,
        "scala": Language.SCALA,
        "swift": Language.SWIFT,
        "tex": Language.LATEX,
        # Add more as needed
    }

    if file_extension in lang_map:
        splitter = RecursiveCharacterTextSplitter.from_language(
            language=lang_map[file_extension], chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    else:
        # Fallback for generic text or unsupported languages
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    
    # Langchain splitters expect a list of Documents or strings.
    # We're passing a single string, so it will be treated as one document to split.
    # The result of split_text is a list of strings (the chunks).
    # We'll convert these back to Langchain Document objects for consistency if needed later.
    docs = splitter.create_documents([file_content], metadatas=[{"source": file_path}])
    return docs


async def embed_chunks(chunks: List[Document]) -> List[List[float]]:
    """Generates embeddings for a list of text chunks (Langchain Documents)."""
    model = get_embeddings_model()
    if not model:
        raise ValueError("Embeddings model not available.")
    
    chunk_contents = [doc.page_content for doc in chunks]
    if not chunk_contents:
        return []
    
    try:
        return await model.aembed_documents(chunk_contents) # Use async version
    except Exception as e:
        print(f"Error during batch embedding: {e}")
        # Fallback to one-by-one if batch fails (though aembed_documents should handle lists)
        # This is more of a conceptual fallback; aembed_documents itself might have retry logic
        # or specific errors that need handling. For now, we'll just re-raise if batch fails.
        raise

def calculate_file_hash(file_content: str) -> str:
    """Calculates SHA256 hash of file content."""
    return hashlib.sha256(file_content.encode('utf-8')).hexdigest()

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


async def delete_chunks_for_file(repo_id: str, file_path: str):
    """Deletes all chunks associated with a specific file in a repo."""
    client = get_supabase_client()
    if not client:
        print(f"Supabase client not available. Cannot delete chunks for {file_path} in {repo_id}")
        return {"error": "Supabase client not available"}
    try:
        data, count = client.table("code_chunks").delete().match({"repo_id": repo_id, "file_path": file_path}).execute()
        print(f"Deleted {count} chunks for file {file_path} in repo {repo_id}")
        return {"data": data, "count": count}
    except Exception as e:
        print(f"Error deleting chunks for file {file_path} in repo {repo_id}: {e}")
        return {"error": str(e)}

async def delete_all_chunks_for_repo(repo_id: str):
    """Deletes all chunks associated with a repository."""
    client = get_supabase_client()
    if not client:
        print(f"Supabase client not available. Cannot delete chunks for repo {repo_id}")
        return {"error": "Supabase client not available"}
    try:
        data, count = client.table("code_chunks").delete().eq("repo_id", repo_id).execute()
        print(f"Deleted {count} chunks for repo {repo_id}")
        return {"data": data, "count": count}
    except Exception as e:
        print(f"Error deleting chunks for repo {repo_id}: {e}")
        return {"error": str(e)}

# Example usage (for testing this module directly, not part of app.py)
if __name__ == '__main__':
    async def main():
        if not supabase or not embeddings_model:
            print("Supabase or Embeddings model not initialized. Exiting test.")
            return

        # Test data
        test_repo_id = "owner/test_repo"
        test_owner = "owner"
        test_repo_name = "test_repo"
        test_commit_sha = "abcdef123456"
        test_file_path = "src/example.py"
        test_file_content = """
def hello_world():
    print("Hello, world!")
    # This is a simple function

class MyClass:
    def __init__(self, name):
        self.name = name

    def greet(self):
        print(f"Hello, {self.name}!")

# More code to make it longer
# More code to make it longer
# ... (repeat to ensure chunking)
        """ * 10 # Make content long enough for multiple chunks

        print(f"Testing with repo_id: {test_repo_id}")

        # 1. Update status to 'indexing'
        await update_repository_indexing_status(test_repo_id, test_owner, test_repo_name, "indexing", test_commit_sha)
        print(f"Repo status updated to indexing.")

        # 2. Chunk content
        langchain_docs = chunk_file_content(test_file_content, test_file_path)
        print(f"File content chunked into {len(langchain_docs)} Langchain documents.")
        for i, doc in enumerate(langchain_docs):
            print(f"  Chunk {i+1} ({len(doc.page_content)} chars): {doc.page_content[:80]}...")


        # 3. Embed chunks
        if langchain_docs:
            try:
                chunk_embeddings = await embed_chunks(langchain_docs)
                print(f"Generated {len(chunk_embeddings)} embeddings, each of dimension {len(chunk_embeddings[0]) if chunk_embeddings else 'N/A'}.")

                # 4. Store chunks
                chunks_to_store = [
                    {"chunk_content": doc.page_content, "embedding": emb}
                    for doc, emb in zip(langchain_docs, chunk_embeddings)
                ]
                await store_code_chunks_batch(test_repo_id, test_commit_sha, test_file_path, chunks_to_store)
                print(f"Stored chunks for {test_file_path}")
            except Exception as e:
                print(f"Error during embedding or storing: {e}")
        else:
            print("No chunks generated, skipping embedding and storing.")


        # 5. Calculate file hash
        current_file_hash = calculate_file_hash(test_file_content)
        print(f"Calculated hash for {test_file_path}: {current_file_hash}")
        file_hashes_map = {test_file_path: current_file_hash}

        # 6. Update status to 'completed'
        await update_repository_indexing_status(test_repo_id, test_owner, test_repo_name, "completed", test_commit_sha, file_hashes_map)
        print(f"Repo status updated to completed.")

        # 7. Check repo status
        status_check = await check_if_repo_indexed(test_repo_id)
        print(f"Checked repo status: {status_check}")

        # 8. Retrieve file hashes
        retrieved_hashes = await get_indexed_file_hashes(test_repo_id)
        print(f"Retrieved file hashes: {retrieved_hashes}")
        
        # 9. Test deletion (optional cleanup)
        # print(f"Attempting to delete chunks for file: {test_file_path}")
        # await delete_chunks_for_file(test_repo_id, test_file_path)
        # print(f"Attempting to delete all chunks for repo: {test_repo_id}")
        # await delete_all_chunks_for_repo(test_repo_id)
        # print(f"Attempting to delete repository record")
        # await supabase.table("indexed_repositories").delete().eq("repo_id", test_repo_id).execute()


    import asyncio
    asyncio.run(main()) 