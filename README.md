# CtrlZ
GitOps AI assistant acting as CtrlZ for production.

Download the app here: https://github.com/apps/ctrlz-gitops

## Features

CtrlZ monitors your GitHub repositories and takes action when workflow runs fail. Here's a breakdown of its process:

1.  **Webhook Event Handling:**
    *   Listens for GitHub webhook events, primarily `workflow_run` (when a run completes with a failure) and `repository` (for initial indexing).
    *   Also monitors `push` events to the default branch to trigger re-indexing of the repository.

2.  **Log Collection & Storage:**
    *   Upon a failed `workflow_run`, automatically fetches the detailed logs for that run.

3.  **Automated Log Analysis (`LogAnalyzer` Agent):**
    *   Processes the collected logs to understand the nature of the error, identify key error messages, and extracts a structured problem statement.

4.  **Contextual Code Retrieval (RAG via Supabase):**
    *   Uses the problem statement from the log analysis to search for relevant code snippets within the indexed repository (leveraging vector embeddings stored in Supabase).
    *   Retrieves the full content of the files corresponding to the most relevant code chunks to provide comprehensive context to the fixing agent.

5.  **AI-Powered Code Correction (`CodeFixer` Agent):**
    *   Combines the structured log analysis and the retrieved relevant code files (RAG context).
    *   Leverages a Large Language Model (LLM) to understand the problem in context and propose a code fix.
    *   The agent is designed to output the complete, corrected file content(s) or instructions to delete files.

6.  **Automated Pull Request Generation:**
    *   If the `CodeFixer` agent generates a viable fix:
        *   A new branch is created from the commit that triggered the workflow failure.
        *   The proposed file changes (creations, modifications, or deletions) are committed to this new branch.
        *   A Pull Request is automatically opened against the repository's default branch.
        *   The PR description includes details about the original problem (from log analysis), a summary of the fix, and a list of modified/deleted files.

7.  **Repository Indexing (`repository_indexer` & Supabase):**
    *   When a new repository is added to the app or when the default branch of an existing repository is updated, the `repository_indexer` processes its content.
    *   Files are chunked, embeddings are generated, and this data is stored in Supabase to enable efficient semantic search for the RAG process.

This automated pipeline aims to quickly address issues, provide developers with a head start on debugging, and reduce the mean time to recovery (MTTR) for production incidents reflected in workflow failures.

## Install (Python/FastAPI)

1. **Clone the repository and enter the directory.**
2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Set environment variables

1. Create a `.env` file in the project root.
2. Add your GitHub App's private key, app ID, LLM APIO key, and webhook secret to the `.env` file:
   ```env
   APP_ID=your_github_app_id
   WEBHOOK_SECRET=your_webhook_secret
   PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----
   GOOGLE_API_KEY=your_api_key
   ```
   (Paste your private key as a single line, replacing newlines with `\n`.)

## Run the server

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```
- The webhook endpoint will be at `http://localhost:8000/api/webhook`.

## Expose to GitHub (for local development)

You can use [smee.io](https://smee.io/) to receive webhooks locally.


1 Set the smee channel URL as your webhook URL in your GitHub App settings.
3. On your local machine, run:
   ```bash
   npx smee-client --url https://smee.io/smee-number --target http://localhost:8000/api/webhook
   ```
- Now, webhooks will be forwarded from smee.io to your local FastAPI server.

Learn more: [smee.io](https://smee.io/)
