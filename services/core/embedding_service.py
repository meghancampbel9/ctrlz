import os
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from config import GOOGLE_API_KEY, EMBEDDING_DIMENSION
from typing import List

# Global Embeddings model instance
embeddings_model_instance: GoogleGenerativeAIEmbeddings | None = None

def get_embeddings_model() -> GoogleGenerativeAIEmbeddings | None:
    global embeddings_model_instance
    if embeddings_model_instance is None:
        if GOOGLE_API_KEY:
            try:
                embeddings_model_instance = GoogleGenerativeAIEmbeddings(
                    model="models/embedding-001",
                    google_api_key=GOOGLE_API_KEY
                )
                print(f"[INFO] GoogleGenerativeAIEmbeddings model initialized successfully (model: models/embedding-001, dimension: {EMBEDDING_DIMENSION}).")
            except Exception as e:
                print(f"[CRITICAL ERROR] Failed to initialize GoogleGenerativeAIEmbeddings model: {e}")
                embeddings_model_instance = None
        else:
            print("[CRITICAL ERROR] GOOGLE_API_KEY not found in config. GoogleGenerativeAIEmbeddings model not initialized.")
            embeddings_model_instance = None
    return embeddings_model_instance

async def embed_chunks(chunks_batch: List[str]) -> List[List[float]] | None:
    """Generates embeddings for a list of text strings."""
    model = get_embeddings_model()
    if not model:
        print("[ERROR] Embeddings model not available for embed_chunks.")
        return None
    if not chunks_batch:
        return []
    try:
        embeddings = await model.aembed_documents(chunks_batch)
        print(f"[INFO] Successfully embedded {len(chunks_batch)} chunks via GoogleGenerativeAIEmbeddings.")
        return embeddings
    except Exception as e:
        print(f"[ERROR] Error embedding chunks via GoogleGenerativeAIEmbeddings: {e}")
        return None