import os
import asyncio
import re
from pathlib import Path
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from typing import List, Dict, Any
from supabase_service import get_workflow_logs_for_run

load_dotenv()


class LogAnalyzer:
    def __init__(self, model_name="gemini-1.5-flash-latest", temperature=0.2):
        """
        Initializes the LogAnalyzer agent.
        Args:
            model_name (str): The name of the Gemini model to use.
            temperature (float): The temperature for the LLM's generation.
        """
        try:
            self.llm = ChatGoogleGenerativeAI(model=model_name, temperature=temperature)
        except Exception as e:
            print(f"Error initializing ChatGoogleGenerativeAI: {e}")
            print("Please ensure your GOOGLE_API_KEY is correctly set in the .env file and valid.")
            raise

        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system", ("""Your SOLE task is to generate a structured Markdown output based on the provided logs. This output will be used as a direct prompt for another AI agent called CodeFixer.

You MUST ONLY output the Markdown structure described below. NOTHING ELSE.
Your response MUST begin *immediately* with `## Problem Statement` and adhere strictly to this format:

## Problem Statement
[Concisely describe the core code-related problem. Analyze the logs to find this. If the failure is due to an explicit command (e.g., `exit 1` in a script), state this. Example: "The workflow failed due to an `exit 1` command in the script executed during the 'Fail the pipeline' step. The CodeFixer should examine the script associated with 'deploy_9_Fail the pipeline.txt' and, if this failure is not intended for testing, remove or modify the 'exit 1' command."]

## Key Log Snippets
[Internally identify the most critical log lines for understanding the error and its context. Then, quote these exact, verbatim log lines. Preserve formatting. Use a Markdown code block.]

## Suspected Files/Components
[Based on your log analysis, list any specific file names (e.g., `deploy_9_Fail the pipeline.txt` if it implies a script) or components that appear directly related to the error. If none, state "Not specifically identified in logs beyond the failing script itself." or similar.]

## Additional Investigation Context (Optional)
[If your log analysis revealed relevant context not directly code-fixable (e.g., "External service X was unresponsive"), mention it here. If there is no such context, OMIT this entire section or write "None.".]

**IMPORTANT RULES FOR YOUR OUTPUT:**
1.  Start your response *immediately* with `## Problem Statement`. No preamble.
2.  Strictly adhere to the specified Markdown headers and structure.
3.  Do NOT include any narrative, greetings, explanations of your analytical process, or summaries outside of the defined sections.
4.  The content within each section should be derived from your internal analysis of the logs.
""")),
            ("user", ("""Analyze the following workflow logs and generate the structured Markdown prompt for CodeFixer, strictly following the format defined in the system message:

```text
{log_content}
```
""")),
        ])
        self.chain = self.prompt_template | self.llm | StrOutputParser()

    async def async_analyze_logs_from_supabase(self, run_id: int, repository_full_name: str) -> str:
        """
        Fetches logs from Supabase for a given run_id and repository,
        combines their content, and sends it to the LLM for analysis.
        Args:
            run_id (int): The GitHub Actions workflow run ID.
            repository_full_name (str): The full name of the repository (e.g., 'owner/repo').

        Returns:
            str: The analysis result from the LLM, or an error message.
        """
        print(f"LogAnalyzer: Fetching logs from Supabase for run {run_id}, repo {repository_full_name}")
        try:
            log_entries = await get_workflow_logs_for_run(run_id, repository_full_name)
        except Exception as e_fetch:
            return f"Error fetching logs from Supabase: {e_fetch}"

        if not log_entries:
            return f"No logs found in Supabase for run {run_id}, repo '{repository_full_name}'."

        all_log_contents = []
        for entry in log_entries:
            log_filename = entry.get('log_filename', 'unknown_log_file.txt')
            content = entry.get('log_content', '')
            job_name = entry.get('job_name', 'N/A')
            workflow_name = entry.get('workflow_name', 'N/A')  # Added workflow name

            header = f"--- Log File: {log_filename} (Job: {job_name}, Workflow: {workflow_name}) ---"
            footer = f"--- End Log File: {log_filename} ---"
            all_log_contents.append(f"{header}\n{content}\n{footer}")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000  # Same truncation as before
        if len(combined_logs) > max_chars:
            print(
                f"Warning (Supabase logs): Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
            combined_logs = combined_logs[:max_chars] + "\n... (logs truncated due to length)"

        if not combined_logs.strip():
            return "Error: All log files from Supabase were empty or unreadable."

        try:
            response = await self.chain.ainvoke({"log_content": combined_logs})
            return response
        except Exception as e_llm:
            return f"Error during LLM invocation with Supabase logs: {e_llm}\nMake sure your GOOGLE_API_KEY is valid and the model is accessible."


class CodeFixer:
    def __init__(self, model_name="gemini-1.5-flash-latest", temperature=0.3):
        """
        Initializes the CodeFixer agent.
        Args:
            model_name (str): The name of the Gemini model to use.
            temperature (float): The temperature for the LLM's generation.
        """
        self.llm = None  # Initialize to None
        self.chain = None  # Initialize to None
        try:
            self.llm = ChatGoogleGenerativeAI(model=model_name, temperature=temperature)
            print(f"CodeFixer agent initialized with model: {model_name}")

            self.prompt_template = ChatPromptTemplate.from_messages([
                ("system", """You are an expert AI assistant specializing in diagnosing and fixing GitHub Actions workflow failures.
Your task is to analyze the provided information and propose a precise code fix by outputting the complete modified file content.

**Input Context:**
1.  **Log Analyzer Report:** This contains a problem statement, key log snippets, and suspected files based on the raw logs.
2.  **Workflow YAML Content:** The full content of the GitHub Actions workflow YAML file that failed. This is provided in `{workflow_yaml_path}`.
3.  **Relevant Code Snippets (RAG):** These are chunks of code from the repository, identified as potentially relevant by a semantic search. Each snippet includes its file path and its *original* content. You should use this original content as the basis for your modifications. If a file mentioned in the Log Analyzer report or workflow YAML is also present in these snippets, prioritize using the content from the RAG snippets as the most accurate original version.
4.  **Target Branch:** The branch where the failure occurred and where the fix should be applied.

**Your Goal:**
Generate the complete, modified content for each file that needs to be changed.

**Output Format Requirements:**
- Your response MUST consist of one or more file blocks, or a delete instruction, or the string "NO_CODE_FIX_POSSIBLE".
- Each file block representing a modification or creation MUST start with a header line: `==BEGIN FILE:/path/to/modified/file.ext==` where `/path/to/modified/file.ext` is the repository-relative path of the file.
- Immediately following the header line, you MUST provide the ENTIRE new content for that file.
- Each file block MUST end with a footer line: `==END FILE:/path/to/modified/file.ext==`.
- Example for a single file modification:
  ```
==BEGIN FILE:src/app.py==
# This is the full new content of src/app.py
# including all original lines that were not changed,
# and all new/modified lines.
print("Hello, corrected world!")
==END FILE:src/app.py==
  ```
- If multiple files need changes, provide multiple such blocks sequentially.
- If the fix involves creating a new file, use the intended new file path in the `==BEGIN FILE:/path/to/new/file.ext==` header and provide its full content.
- If the fix involves deleting a file, you MUST output ONLY a single line in the format: `==DELETE FILE:/path/to/delete/file.ext==`. If multiple files are to be deleted, provide one such line for each.
- Ensure the file paths are relative to the repository root.

**Analysis and Fix Generation Steps:**
1.  **Understand the Core Problem:** Use the "Problem Statement" from the Log Analyzer Report.
2.  **Corroborate with Workflow YAML:** Examine the `workflow_yaml_content`. Its path is `{workflow_yaml_path}`. If this file needs changes, use this path in your output.
3.  **Leverage RAG Snippets:**
    *   Review `relevant_code_snippets`. These provide the original content of potentially relevant files.
    *   Identify the file(s) to modify based on the problem and the RAG snippets.
4.  **Formulate the Fix:**
    *   Determine the necessary changes.
    *   Construct the *complete new content* for each modified file. This means if a file is 100 lines long and you change 2 lines, you output all 100 lines with the 2 changes incorporated.
    *   If the workflow YAML itself is the problem (its path is `{workflow_yaml_path}`), provide its complete modified content using its path.
5.  **Construct the Output:** Adhere strictly to the `==BEGIN FILE...==` / `==END FILE...==` or `==DELETE FILE...==` format.

**Important Considerations:**
- **Specificity:** Your proposed fix MUST be the complete file content(s) or delete instructions in the specified format. Do not provide explanations, apologies, or general advice. ONLY the specified file blocks, delete instructions, or "NO_CODE_FIX_POSSIBLE".
- **File Paths:** Ensure the file paths in your `==BEGIN FILE:/path/to/file==`, `==END FILE:/path/to/file==`, and `==DELETE FILE:/path/to/file==` markers are accurate and relative to the repository root. The RAG snippets provide paths for files they contain, and the workflow YAML path is `{workflow_yaml_path}`.
- **No Fix Possible:** If, after careful analysis, you determine that a code fix is not possible based on the provided information, output ONLY the following string:
  `NO_CODE_FIX_POSSIBLE`

**Target Branch Context:** The fix will be applied to the branch: `{target_branch}`. This is for your context; do not include it in your output.
"""),
                ("user", """Analyze the following information and generate the full modified file content(s) or delete instructions.

**1. Log Analyzer Report:**
```markdown
{log_analyzer_output}
```

**2. Workflow YAML Content (`{workflow_yaml_path}`):**
```yaml
{workflow_yaml_content}
```

**3. Relevant Code Snippets (from RAG search - these are the original versions of files in the codebase):**
{rag_snippets_formatted}

Reminder: Your output must be ONLY the full file content block(s) in the specified `==BEGIN FILE...==` / `==END FILE...==` format, `==DELETE FILE...==` instructions, or the string `NO_CODE_FIX_POSSIBLE`.""")
            ])
            self.chain = self.prompt_template | self.llm | StrOutputParser()

        except Exception as e:
            print(f"Error initializing CodeFixer LLM or prompt template: {e}")
            # self.llm and self.chain remain None if an error occurs
            # Potentially re-raise or handle more gracefully if needed

    async def propose_fix(
            self,
            log_analyzer_output: str,
            relevant_code_snippets: List[Dict[str, Any]],
            workflow_yaml_content: str,  # Full content of the workflow file
            workflow_yaml_path: str,  # Path to the workflow file, e.g., .github/workflows/main.yml
            target_branch: str
    ) -> str:
        """
        Analyzes the error report, workflow YAML, and relevant code snippets to propose a fix
        by returning the full content of modified files or a delete instruction.
        Args:
            log_analyzer_output (str): The structured Markdown output from LogAnalyzer.
            relevant_code_snippets (List[Dict[str, Any]]): Top k relevant code snippets from RAG.
                                                           Each snippet should contain 'file_path' and 'chunk_content' (original full content).
            workflow_yaml_content (str): The content of the workflow YAML file associated with the failed run.
            workflow_yaml_path (str): The repository-relative path of the workflow YAML file.
            target_branch (str): The branch on which the failure occurred.
        Returns:
            str: A string containing the LLM's direct response, which is expected to be
                 one or more file content blocks (e.g., ==BEGIN FILE...== ... ==END FILE...==),
                 delete instructions (e.g., ==DELETE FILE...==), or "NO_CODE_FIX_POSSIBLE".
        """
        print("\n--- CodeFixer: Propose Fix (Outputting Full Files) --- ")

        if not self.chain:
            return "CodeFixer LLM chain not initialized. Cannot propose fix."

        rag_snippets_formatted = "No relevant code snippets from the codebase were provided for context."
        if relevant_code_snippets:
            formatted_list = []
            for i, snippet in enumerate(relevant_code_snippets):
                snippet_path = snippet.get('file_path', 'Unknown file')
                # The content here is the *original* content from RAG, which the LLM should use as a base.
                content = snippet.get('chunk_content', '')
                similarity = snippet.get('similarity', 0.0)  # Similarity might be less relevant now but retain for info
                formatted_list.append(
                    f"Snippet {i + 1}: File: `{snippet_path}` (Similarity: {similarity:.4f})\n"
                    f"Original Content of `{snippet_path}`:\n"
                    f"```\n{content}\n```"  # Ensure RAG provides full file content for this to be effective
                )
            rag_snippets_formatted = "\n\n".join(formatted_list)

        # Prepare inputs for the prompt
        prompt_inputs = {
            "log_analyzer_output": log_analyzer_output,
            "workflow_yaml_content": workflow_yaml_content.strip() if workflow_yaml_content else "Workflow YAML content not available.",
            "workflow_yaml_path": workflow_yaml_path if workflow_yaml_path else "Workflow YAML path not available.",
            "rag_snippets_formatted": rag_snippets_formatted,
            "target_branch": target_branch
        }

        print(f"CodeFixer: Invoking LLM for branch '{target_branch}'. Expecting full file content output.")
        # For debugging the exact input to the LLM if needed:
        # print("--- CodeFixer Prompt Input ---")
        # current_prompt_string = self.prompt_template.format_prompt(**prompt_inputs).to_string()
        # print(current_prompt_string)
        # print("--- End CodeFixer Prompt Input ---")

        try:
            response = await self.chain.ainvoke(prompt_inputs)
            print("--- CodeFixer LLM Raw Response ---")
            # Limit printing very long responses to keep logs cleaner
            if len(response) > 2000:
                print(response[:1000] + "\n... (response truncated in log) ...\n" + response[-1000:])
            else:
                print(response)
            print("--- End CodeFixer LLM Raw Response ---")

            # Basic validation: Check if it's "NO_CODE_FIX_POSSIBLE" or seems to contain our markers
            response_stripped = response.strip()
            if response_stripped == "NO_CODE_FIX_POSSIBLE":
                print("CodeFixer: LLM responded NO_CODE_FIX_POSSIBLE.")
                return response_stripped
            # Check for BEGIN/END blocks OR DELETE FILE instructions
            has_begin_end_markers = "==BEGIN FILE:" in response and "==END FILE:" in response
            has_delete_marker = "==DELETE FILE:" in response_stripped
            # Allow responses that are just a single delete instruction
            is_single_delete_instruction = has_delete_marker and response_stripped.startswith(
                "==DELETE FILE:") and response_stripped.count('\n') == 0

            if has_begin_end_markers or is_single_delete_instruction:
                if has_begin_end_markers:
                    print("CodeFixer: LLM response appears to contain file content blocks.")
                if is_single_delete_instruction:
                    print("CodeFixer: LLM response appears to be a single delete file instruction.")
                # We will return the raw response for app.py to parse into individual files.
                return response  # Return the full response, not just stripped
            elif has_delete_marker:  # has delete marker but not a clean single line or part of begin/end
                print("CodeFixer: LLM response appears to contain delete file instruction(s).")
                return response  # Return the full response

            else:
                print(
                    f"CodeFixer Warning: LLM response did not conform to expected output structure (NO_CODE_FIX_POSSIBLE, BEGIN/END FILE blocks, or DELETE FILE). Response was: '{response_stripped[:500]}...'")
                return f"# LLM_RESPONSE_UNEXPECTED_FORMAT\n{response}"

        except Exception as e:
            print(f"Error during CodeFixer LLM invocation: {e}")
            import traceback
            traceback.print_exc()
            return "Error during CodeFixer LLM analysis."
        finally:
            print("\n--- End CodeFixer --- \n")
