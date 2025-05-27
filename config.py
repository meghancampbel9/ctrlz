import os
from dotenv import load_dotenv

load_dotenv()

# GitHub App Configuration
APP_ID = os.getenv("APP_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY")

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# Google Cloud / Vertex AI Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # For Vertex AI Embeddings, etc.
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") # Keep for potential VertexAI embedding future use
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "768"))

# --- Derived/Processed Configuration ---
PRIVATE_KEY = None
if PRIVATE_KEY_PATH:
    try:
        with open(PRIVATE_KEY_PATH, "r") as f:
            PRIVATE_KEY = f.read()
    except FileNotFoundError:
        print(f"[CRITICAL CONFIG ERROR] Private key file not found at path: {PRIVATE_KEY_PATH}.")
        PRIVATE_KEY = None # Ensure it's None if not found
    except Exception as e:
        print(f"[CRITICAL CONFIG ERROR] Error reading private key file: {e}")
        PRIVATE_KEY = None # Ensure it's None on error

# --- Validation & Logging --- 
def get_config_diagnostics():
    config_vars = {
        "APP_ID": APP_ID,
        "WEBHOOK_SECRET": "******" if WEBHOOK_SECRET else None, # Mask sensitive
        "PRIVATE_KEY_PATH": PRIVATE_KEY_PATH,
        "PRIVATE_KEY_LOADED": True if PRIVATE_KEY else False,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_KEY": "******" if SUPABASE_SERVICE_KEY else None, # Mask sensitive
        "GOOGLE_API_KEY": "******" if GOOGLE_API_KEY else None, # Mask sensitive
        "GOOGLE_CLOUD_PROJECT": GOOGLE_CLOUD_PROJECT, # Kept for info
        "EMBEDDING_DIMENSION": EMBEDDING_DIMENSION
    }
    missing_critical = []
    if not APP_ID: missing_critical.append("APP_ID")
    if not WEBHOOK_SECRET: missing_critical.append("WEBHOOK_SECRET")
    if not PRIVATE_KEY: missing_critical.append("PRIVATE_KEY (from PRIVATE_KEY_PATH)")
    if not SUPABASE_URL: missing_critical.append("SUPABASE_URL")
    if not SUPABASE_SERVICE_KEY: missing_critical.append("SUPABASE_SERVICE_KEY")
    if not GOOGLE_API_KEY: missing_critical.append("GOOGLE_API_KEY")
    # if not GOOGLE_CLOUD_PROJECT: missing_critical.append("GOOGLE_CLOUD_PROJECT") # No longer critical for embeddings
    
    print("--- Configuration Diagnostics ---")
    for key, value in config_vars.items():
        print(f"  {key}: {value}")
    if missing_critical:
        print(f"[CRITICAL CONFIG WARNING] The following critical config variables are missing or not loaded: {', '.join(missing_critical)}")
        print("  Application functionality will be severely impacted.")
    else:
        print("  All critical configurations appear to be loaded.")
    print("-------------------------------")
    return config_vars, missing_critical