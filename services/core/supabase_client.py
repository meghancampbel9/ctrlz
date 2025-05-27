import os
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY

# Global Supabase client instance
supabase_client: Client | None = None

def get_supabase_client() -> Client | None:
    global supabase_client
    if supabase_client is None:
        if SUPABASE_URL and SUPABASE_SERVICE_KEY:
            try:
                supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
                print("[INFO] Supabase client initialized successfully.")
            except Exception as e:
                print(f"[CRITICAL ERROR] Failed to initialize Supabase client: {e}")
                supabase_client = None # Ensure it remains None on failure
        else:
            print("[CRITICAL ERROR] SupABASE_URL or SUPABASE_SERVICE_KEY not found in config. Supabase client not initialized.")
            supabase_client = None # Ensure it remains None if config is missing
    return supabase_client