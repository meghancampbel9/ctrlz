import hmac
import hashlib
import json
import traceback
from fastapi import Request, Header, HTTPException, BackgroundTasks, APIRouter
import base64 
from config import WEBHOOK_SECRET, SUPABASE_URL, SUPABASE_SERVICE_KEY, GOOGLE_API_KEY
from .api import (
    fetch_and_store_workflow_logs,
    get_github_file_details,
    create_github_branch,
    delete_github_branch,
    commit_file_to_github,
    delete_github_file,
    create_github_pull_request_api
)
from agents.log_analyzer_agent import LogAnalyzer
from agents.code_fixer import CodeFixer
from services.db.code_chunks_service import get_full_file_content_from_chunks
from rag.retriever import search_relevant_code_chunks
from rag.indexer import process_repository

if not WEBHOOK_SECRET:
    print("[CRITICAL ERROR] WEBHOOK_SECRET not configured via config.py for webhooks.py.")

router = APIRouter()

def verify_signature(payload: bytes, signature: str | None):
    if not WEBHOOK_SECRET:
        print("[ERROR] WEBHOOK_SECRET not configured (from config). Cannot verify signature.")
        return False
    if not signature:
        print("[ERROR] Signature not provided in webhook.")
        return False
    try:
        sha_name, signature_hex = signature.split('=', 1)
    except ValueError:
        print(f"[ERROR] Malformed signature header: {signature}")
        return False
    if sha_name != 'sha256':
        print(f"[ERROR] Signature algorithm not sha256: {sha_name}")
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature_hex)

import re
def parse_codefixer_output_to_files(llm_output: str) -> list[dict[str, str]]:
    operations = []
    delete_pattern = re.compile(r"^==DELETE FILE:(?P<file_path>.*?)==$", re.MULTILINE)
    upsert_pattern = re.compile(r"^==BEGIN FILE:(?P<file_path_begin>.*?)==$(?P<content>.*?)^==END FILE:(?P<file_path_end>.*?)==$", re.MULTILINE | re.DOTALL)
    all_matches = []
    for match in delete_pattern.finditer(llm_output):
        all_matches.append({'type': 'delete', 'match': match})
    for match in upsert_pattern.finditer(llm_output):
        all_matches.append({'type': 'upsert', 'match': match})
    all_matches.sort(key=lambda x: x['match'].start())
    for item in all_matches:
        match_type = item['type']
        match_obj = item['match']
        file_path_str = ""
        if match_type == 'delete':
            file_path_str = match_obj.group("file_path").strip()
            if file_path_str:
                operations.append({"action": "delete", "file_path": file_path_str})
            else:
                print(f"[CodeFixer Output Parser Warning] Found DELETE marker with empty file path.")
        elif match_type == 'upsert':
            file_path_begin_str = match_obj.group("file_path_begin").strip()
            file_path_end_str = match_obj.group("file_path_end").strip()
            content_str = match_obj.group("content")
            if content_str.startswith('\n'):
                content_str = content_str[1:]
            if content_str.endswith('\n'):
                content_str = content_str[:-1]
            if file_path_begin_str and file_path_begin_str == file_path_end_str:
                operations.append({"action": "upsert", "file_path": file_path_begin_str, "content": content_str})
            else:
                print(f"[CodeFixer Output Parser Warning] Mismatched or empty file paths: BEGIN='{file_path_begin_str}', END='{file_path_end_str}'")
    if not operations and llm_output.strip() not in ["NO_CODE_FIX_POSSIBLE", ""] and not llm_output.strip().startswith("# LLM_RESPONSE_UNEXPECTED_FORMAT"):
        print(f"[CodeFixer Output Parser Warning] No ops parsed. Output: {llm_output[:500]}")
    return operations

@router.post("/api/webhook")
async def github_webhook_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None)
):
    print(f"Received event: {x_github_event}")

    raw_body = await request.body()
    if not verify_signature(raw_body, x_hub_signature_256):
        print("Error: Webhook signature verification failed.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    action = payload.get("action")
    installation_id = payload.get("installation", {}).get("id")
    repository = payload.get("repository", {})
    owner = repository.get("owner", {}).get("login")
    repo_name = repository.get("name")

    print(f"Processing webhook for: {owner}/{repo_name}, Event: {x_github_event}, Action: {action}, Installation ID: {installation_id}")

    if not all([owner, repo_name, installation_id]):
        print(f"Error: Missing owner, repo_name, or installation_id. Event: {x_github_event}")
        return {"ok": False, "error": "Incomplete payload for repository identification."}
    
    # Use config for service readiness checks
    supabase_ready = SUPABASE_URL and SUPABASE_SERVICE_KEY
    embeddings_ready = GOOGLE_API_KEY

    if x_github_event == "repository" and action == "created":
        print(f"New repository created: {owner}/{repo_name}. Triggering indexing.")
        if supabase_ready and embeddings_ready:
            repo_url = repository.get("clone_url")
            if not repo_url:
                 print(f"[ERROR] Clone URL missing for new repository {owner}/{repo_name}")
                 return {"ok": False, "error": "Clone URL missing."}
            background_tasks.add_task(process_repository, repo_url, f"{owner}/{repo_name}", owner, repo_name, repository.get("default_branch", "main"), installation_id)
            return {"ok": True, "message": "Repository indexing initiated."}
        else:
            print("[ERROR] Supabase or Embeddings model not available (checked via config). Cannot initiate repository indexing.")
            return {"ok": False, "error": "Backend services not ready for indexing."}

    elif x_github_event == "push":
        ref = payload.get("ref", "")
        default_branch = repository.get("default_branch", "main")
        if ref == f"refs/heads/{default_branch}":
            print(f"Push event to default branch {default_branch} for {owner}/{repo_name}. Triggering re-indexing.")
            if supabase_ready and embeddings_ready:
                repo_url = repository.get("clone_url")
                if not repo_url:
                    print(f"[ERROR] Clone URL missing for push to {owner}/{repo_name}")
                    return {"ok": False, "error": "Clone URL missing."}
                background_tasks.add_task(process_repository, repo_url, f"{owner}/{repo_name}", owner, repo_name, default_branch, installation_id)
                return {"ok": True, "message": "Repository re-indexing due to push to default branch."}
            else:
                print("[ERROR] Supabase or Embeddings model not available (checked via config). Cannot initiate re-indexing.")
                return {"ok": False, "error": "Backend services not ready for re-indexing."}
        else:
            print(f"Push event to ref {ref}. Not the default branch ({default_branch}). Skipping re-indexing.")
            return {"ok": True, "message": "Push to non-default branch, no re-indexing."}

    elif x_github_event == "workflow_run":
        action = payload.get("action")
        workflow_run_info = payload.get("workflow_run", {})
        status = workflow_run_info.get("status")
        conclusion = workflow_run_info.get("conclusion")
        run_id = workflow_run_info.get("id")

        if not all([action, status, run_id, owner, repo_name, installation_id]):
            print(f"Error: Incomplete workflow_run payload for {owner}/{repo_name}. Skipping.")
            return {"ok": False, "error": "Incomplete payload"}

        if action == "requested":
            print(f"Workflow run {run_id} in {owner}/{repo_name} has been requested.")
            return {"ok": True, "message": "Workflow run requested acknowledgement"}
        
        negative_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
        if status == "completed" and conclusion in negative_conclusions:
            print(f"Workflow run {run_id} in {owner}/{repo_name} ended with '{conclusion}'. Initiating CodeFixer.")

            workflow_yaml_content = ""
            target_branch_for_fix = "unknown"
            workflow_path = None
            current_head_sha = workflow_run_info.get("head_sha")
            workflow_name_from_payload = payload.get("workflow", {}).get("name")

            if not current_head_sha:
                print(f"[ERROR] Critical: `head_sha` missing for run {run_id}.")
                return {"ok": False, "error": "Missing head_sha."}

            workflow_path = payload.get("workflow", {}).get("path")
            if workflow_path:
                workflow_file_details = await get_github_file_details(owner, repo_name, workflow_path, current_head_sha, installation_id)
                if workflow_file_details and workflow_file_details.get("content") and workflow_file_details.get("encoding") == "base64":
                    workflow_yaml_content = base64.b64decode(workflow_file_details["content"]).decode('utf-8')
                else:
                    print(f"[WARN] Failed to fetch workflow YAML {workflow_path}.")
            
            target_branch_for_fix = workflow_run_info.get("head_branch", repository.get("default_branch", "main"))

            logs_ok = await fetch_and_store_workflow_logs(owner, repo_name, run_id, installation_id, workflow_name_from_payload)
            if not logs_ok:
                 print(f"[WARN] Log fetching/storage for run {run_id} not fully successful.")
            
            if not (embeddings_ready and supabase_ready):
                print("[ERROR] Core services not ready for CodeFixer (checked via config).")
                return {"ok": False, "error": "Backend services not ready for CodeFixer."}

            try:
                log_analyzer = LogAnalyzer()
                codefixer_prompt_input = await log_analyzer.async_analyze_logs_from_supabase(run_id, f"{owner}/{repo_name}")
                
                if codefixer_prompt_input.startswith("Error:"):
                    print(f"LogAnalyzer error for run {run_id}: {codefixer_prompt_input}")
                    return {"ok": True, "message": "LogAnalyzer error, CodeFixer skipped."}

                search_query_text = ""
                problem_statement_start = codefixer_prompt_input.find("## Problem Statement")
                if problem_statement_start != -1:
                    search_query_text = codefixer_prompt_input[problem_statement_start + len("## Problem Statement"):].split("##")[0].strip()
                
                full_file_code_context_for_fixer = []
                if search_query_text:
                    relevant_chunks = await search_relevant_code_chunks(f"{owner}/{repo_name}", search_query_text)
                    processed_files = set()
                    for chunk in relevant_chunks:
                        file_path, commit_sha_chunk = chunk.get("file_path"), chunk.get("commit_sha", current_head_sha)
                        if file_path and (file_path, commit_sha_chunk) not in processed_files:
                            content = await get_full_file_content_from_chunks(f"{owner}/{repo_name}", file_path, commit_sha_chunk)
                            if content is not None:
                                full_file_code_context_for_fixer.append({"file_path": file_path, "chunk_content": content, "commit_sha": commit_sha_chunk, "similarity": chunk.get("similarity")})
                                processed_files.add((file_path, commit_sha_chunk))
                
                code_fixer = CodeFixer()
                proposed_fix = await code_fixer.propose_fix(codefixer_prompt_input, full_file_code_context_for_fixer, workflow_yaml_content, workflow_path or "unknown.yml", target_branch_for_fix)

                if proposed_fix and proposed_fix.strip() != "NO_CODE_FIX_POSSIBLE" and not proposed_fix.startswith("Error:") and not proposed_fix.startswith("# LLM_RESPONSE_UNEXPECTED_FORMAT"):
                    new_branch_name = f"codefixer-run-{run_id}-{current_head_sha[:7]}"
                    pr_title = f"CTRL Z Auto-Fix for Workflow Run {run_id} ({target_branch_for_fix})"
                    base_pr_branch = repository.get("default_branch", "main")
                    file_ops = parse_codefixer_output_to_files(proposed_fix)

                    if not file_ops:
                        print(f"[WARN] CodeFixer output parsed no file ops: {proposed_fix[:200]}...")
                    else:
                        await delete_github_branch(owner, repo_name, new_branch_name, installation_id)
                        await create_github_branch(owner, repo_name, new_branch_name, current_head_sha, installation_id)
                        committed_paths, deleted_paths = [], []

                        for op in file_ops:
                            op_path, op_action = op["file_path"], op['action']
                            msg = f"CodeFixer: {op_action} {op_path} for run {run_id}"
                            orig_sha = (await get_github_file_details(owner, repo_name, op_path, current_head_sha, installation_id) or {}).get("sha")

                            if op_action == "upsert":
                                await commit_file_to_github(owner, repo_name, new_branch_name, op_path, op["content"], msg, installation_id, orig_sha)
                                committed_paths.append(op_path)
                            elif op_action == "delete" and orig_sha:
                                await delete_github_file(owner, repo_name, new_branch_name, op_path, msg, installation_id, orig_sha)
                                deleted_paths.append(op_path)
                            elif op_action == "delete":
                                print(f"[ERROR] Cannot delete '{op_path}': original SHA not found.")
                        
                        if committed_paths or deleted_paths:
                            problem_statement_for_pr = "Could not automatically parse the problem statement from logs."
                            if "## Problem Statement" in codefixer_prompt_input:
                                try:
                                    problem_start_idx = codefixer_prompt_input.find("## Problem Statement") + len("## Problem Statement")
                                    problem_end_idx = codefixer_prompt_input.find("## Key Log Snippets", problem_start_idx)
                                    if problem_end_idx == -1: problem_end_idx = len(codefixer_prompt_input)
                                    parsed_problem = codefixer_prompt_input[problem_start_idx:problem_end_idx].strip()
                                    if parsed_problem: 
                                        problem_statement_for_pr = parsed_problem[:800] + ("..." if len(parsed_problem) > 800 else "")
                                except Exception as e_parse_pr_body:
                                    print(f"[WARN] Minor error parsing problem statement for PR body: {e_parse_pr_body}")
                                    pass

                            pr_body_files_list_committed = "\n".join([f"- `{f}`" for f in committed_paths])
                            pr_body_files_list_deleted = "\n".join([f"- Deleted: `{f}`" for f in deleted_paths])
                            
                            files_summary_pr = ""
                            if committed_paths: 
                                files_summary_pr += f"**Files Modified/Created:**\n{pr_body_files_list_committed}"
                            if deleted_paths:
                                if files_summary_pr: 
                                    files_summary_pr += "\n\n"
                                files_summary_pr += f"**Files Deleted:**\n{pr_body_files_list_deleted}"

                            pr_body = (
                                f"This PR contains automated fixes proposed by the CodeFixer agent for a workflow failure on branch `{target_branch_for_fix}` "
                                f"(triggered by commit `{current_head_sha[:7]}`).\n\n"
                                f"**Problem Statement:**\n"
                                f"> {problem_statement_for_pr.replace('\n', '\n> ')}\n\n"
                                f"{files_summary_pr}\n\n"
                                f"Please review the applied changes carefully."
                            )
                            await create_github_pull_request_api(owner, repo_name, new_branch_name, base_pr_branch, pr_title, pr_body, installation_id)
            except Exception as e_pipeline:
                print(f"[CRITICAL] Error in CodeFixer pipeline for run {run_id}: {e_pipeline}")
                traceback.print_exc()
        
        else:
            print(f"Workflow run {run_id} action '{action}', status '{status}', conclusion '{conclusion}'. No CodeFixer action.")

    return {"ok": True}
