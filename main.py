import os
from fastapi import FastAPI, Request, Header, HTTPException, BackgroundTasks
import asyncio 
import re 
from services.core.supabase_client import get_supabase_client
from services.core.embedding_service import get_embeddings_model

# GitHub App logic (webhooks router)
from github.webhooks import router as github_webhook_router

# Initialize Supabase and Embeddings model status check
supabase_client = get_supabase_client()
embeddings_model_instance = get_embeddings_model()

if not supabase_client:
    print("[CRITICAL ERROR] Supabase client could not be initialized. Check Supabase URL/Key.")
if not embeddings_model_instance:
    print("[CRITICAL ERROR] Embeddings model could not be initialized. Check GOOGLE_API_KEY.")

# --- FastAPI Application Setup ---
app = FastAPI(
    title="CTRL Z GitHub AI Assistant",
    description="An AI assistant to analyze GitHub workflow failures and propose fixes.",
    version="0.1.0"
)

# Include the webhook router
app.include_router(github_webhook_router)

# --- Health Check Endpoint ---
@app.get("/health", tags=["Infrastructure"]) # Added a tag for better OpenAPI docs
async def health_check():
    """Basic health check to confirm the service is running."""
    return {
        "status": "ok", 
        "message": "CTRL Z service is running.",
        # "supabase_connection": "healthy" if supabase_ok else "degraded",
        # "embedding_service": "healthy" if embeddings_ok else "degraded"
    }

# --- Main Execution (for running with Uvicorn) ---
# For local development.
# In production, Uvicorn would be started pointing to this file, e.g., uvicorn main:app --reload
if __name__ == "__main__":
    import uvicorn
    print("Starting Uvicorn server for CTRL Z app...")
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True, log_level="info")
