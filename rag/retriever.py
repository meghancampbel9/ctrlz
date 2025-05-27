from services.core.supabase_client import get_supabase_client
from services.core.embedding_service import embed_chunks # Using the new embed_chunks
from config import EMBEDDING_DIMENSION # For vector search query

async def search_relevant_code_chunks(
    repo_id: str, 
    query_text: str, 
    top_k: int = 5, 
    similarity_threshold: float = 0.5 
) -> list[dict]:
    """
    Searches for relevant code chunks in the Supabase vector store.
    Uses the global Supabase client and embeddings model.
    """
    supabase = get_supabase_client()
    if not supabase:
        print("[ERROR][Retriever] Supabase client not available for searching chunks.")
        return []

    # Embed the query text
    query_embedding_list = await embed_chunks([query_text])
    if not query_embedding_list or not query_embedding_list[0]:
        print("[ERROR][Retriever] Failed to generate embedding for query text.")
        return []
    query_embedding = query_embedding_list[0]

    try:
        response = await supabase.rpc(
            'match_code_chunks', 
            {
                'query_embedding': query_embedding,
                'match_threshold': similarity_threshold, 
                'match_count': top_k,
                'p_repo_id': repo_id 
            }
        ).execute()
        
        if response.data:
            print(f"[INFO][Retriever] Found {len(response.data)} relevant chunks for query in repo '{repo_id}'.")
            # The response.data should be a list of dicts, each representing a chunk
            return response.data
        else:
            print(f"[INFO][Retriever] No relevant chunks found for query in repo '{repo_id}'. Response: {response}")
            return []

    except Exception as e:
        print(f"[ERROR][Retriever] Error searching for relevant code chunks in repo '{repo_id}': {e}")
        return []