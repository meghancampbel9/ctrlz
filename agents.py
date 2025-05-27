import os
import asyncio
import re
from pathlib import Path
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from typing import List, Dict, Any

load_dotenv()


class LogAnalyzer:
    def __init__(self, model_name="gemini-2.5-pro-preview-03-25", temperature=0.2):
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

    def analyze_log_directory(self, log_directory_path: str) -> str:
        """
        Reads all .txt log files from the specified directory,
        combines their content, and sends it to the LLM for analysis.

        Args:
            log_directory_path (str): The path to the directory containing .txt log files.

        Returns:
            str: The analysis result from the LLM, or an error message.
        """
        log_dir = Path(log_directory_path)
        if not log_dir.is_dir():
            return f"Error: Log directory '{log_directory_path}' not found or is not a directory."

        all_log_contents = []
        log_files = sorted(log_dir.glob("*.txt"))

        if not log_files:
            return f"No .txt log files found in '{log_directory_path}'."

        for log_file_path in log_files:
            try:
                content = log_file_path.read_text(encoding="utf-8")
                all_log_contents.append(
                    f"--- Log File: {log_file_path.name} ---\n{content}\n--- End Log File: {log_file_path.name} ---")
            except Exception as e:
                all_log_contents.append(
                    f"--- Error reading log file: {log_file_path.name} ---\n{str(e)}\n--- End Error ---")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000
        if len(combined_logs) > max_chars:
            print(
                f"Warning: Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
            combined_logs = combined_logs[:max_chars] + "\n... (logs truncated due to length)"

        if not combined_logs.strip():
            return "Error: All log files were empty or unreadable."

        try:
            response = self.chain.invoke({"log_content": combined_logs})
            return response
        except Exception as e:
            return f"Error during LLM invocation: {e}\nMake sure your GOOGLE_API_KEY is valid and the model is accessible."

    async def async_analyze_log_directory(self, log_directory_path: str) -> str:
        """
        Asynchronously reads log files and sends to LLM for analysis.
        """
        log_dir = Path(log_directory_path)
        if not log_dir.is_dir():
            return f"Error: Log directory '{log_directory_path}' not found or is not a directory."

        all_log_contents = []
        # File I/O is blocking, run in executor for async context
        # However, Path.glob is not easily awaitable. For simplicity, we keep this part sync
        # but it should be fast.
        log_files = await asyncio.to_thread(lambda: sorted(log_dir.glob("*.txt")))

        if not log_files:
            return f"No .txt log files found in '{log_directory_path}'."

        for log_file_path in log_files:
            try:
                # read_text is blocking, run in thread for async compatibility
                content = await asyncio.to_thread(log_file_path.read_text, encoding="utf-8")
                all_log_contents.append(
                    f"--- Log File: {log_file_path.name} ---\n{content}\n--- End Log File: {log_file_path.name} ---")
            except Exception as e:
                all_log_contents.append(
                    f"--- Error reading log file: {log_file_path.name} ---\n{str(e)}\n--- End Error ---")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000
        if len(combined_logs) > max_chars:
            print(
                f"Warning (async): Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
            combined_logs = combined_logs[:max_chars] + "\n... (logs truncated due to length)"

        if not combined_logs.strip():
            return "Error: All log files were empty or unreadable."

        try:
            # Use ainvoke for the asynchronous call
            response = await self.chain.ainvoke({"log_content": combined_logs})
            return response
        except Exception as e:
            return f"Error during LLM invocation (async): {e}\nMake sure your GOOGLE_API_KEY is valid and the model is accessible."


class CodeFixer:
    def __init__(self, model_name="gemini-2.5-pro-preview-03-25", temperature=0.3):
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
Your task is to analyze the provided information and propose a precise code fix.

**Input Context:**
1.  **Log Analyzer Report:** This contains a problem statement, key log snippets, and suspected files based on the raw logs.
2.  **Workflow YAML Content:** The full content of the GitHub Actions workflow YAML file that failed.
3.  **Relevant Code Snippets (RAG):** These are chunks of code from the repository, identified as potentially relevant by a semantic search. Each snippet includes its file path and content.
4.  **Target Branch:** The branch where the failure occurred and where the fix should be applied.

**Your Goal:**
Generate a concise and actionable code fix.

**Output Format Requirements:**
Your response MUST be a Markdown block containing ONLY a code diff in the following format:
```diff
--- a/path/to/original/file.py
+++ b/path/to/modified/file.py
@@ -line_start,num_lines @@ -line_start,num_lines @@
 # ... some context lines (unchanged) ...
 -old_line_to_remove
 +new_line_to_add
 # ... more context lines (unchanged) ...
```
- **Context Lines:** Unchanged lines shown for context MUST start with a single space character. For example: ` unchanged_line_content`.
- If multiple files need changes, provide multiple diff blocks.
- If the fix involves creating a new file, use `/dev/null` for the `--- a/` path.
- If the fix involves deleting a file, use `/dev/null` for the `+++ b/` path.
- Ensure the file paths in the diff are relative to the repository root.

**Analysis and Fix Generation Steps:**
1.  **Understand the Core Problem:** Use the "Problem Statement" from the Log Analyzer Report as your starting point.
2.  **Corroborate with Workflow YAML:** Examine the `workflow_yaml_content`. Identify the specific job and step that failed. The Log Analyzer's "Suspected Files/Components" might point to a script executed by a step in this YAML.
3.  **Leverage RAG Snippets:**
    *   Review the `relevant_code_snippets`. These are your primary source for identifying the actual code to modify.
    *   The file paths in the snippets are crucial. Prioritize fixes in files that appear in both the RAG snippets and are logically connected to the failing workflow step (e.g., a script run by `run:` command in the YAML).
4.  **Formulate the Fix:**
    *   Determine the *exact* lines that need to change.
    *   If a script run by the workflow failed (e.g., identified via `deploy_X_script_name.txt` in logs and RAG snippets of that script), propose changes to that script.
    *   If the workflow YAML itself is the problem, propose changes to `workflow_yaml_content` (using its original path, likely `.github/workflows/your_workflow_file.yml`).
5.  **Construct the Diff:** Adhere strictly to the diff format. Include a few lines of context around your changes.
    - **Hunk Headers Accuracy:** The line numbers in `@@ -original_start,original_length +new_start,new_length @@` hunk headers MUST be accurate relative to the start of the *entire original file content* provided in the `workflow_yaml_content` or RAG snippets. Incorrect hunk headers will lead to failed patch application.

**Important Considerations:**
- **Specificity:** Your proposed fix MUST be a concrete code change. Do not provide explanations, apologies, or general advice. ONLY the diff.
- **File Paths:** Ensure the file paths in your diff (`--- a/path/to/file` and `+++ b/path/to/file`) are accurate and relative to the repository root. The RAG snippets provide these paths.
- **No Fix Possible:** If, after careful analysis, you determine that a code fix is not possible based on the provided information (e.g., external service issue, transient error not code-related, insufficient context despite RAG), output ONLY the following string:
  `NO_CODE_FIX_POSSIBLE`

**Target Branch Context:** The fix will be applied to the branch: `{target_branch}`. This is for your context; do not include it in the diff output.
"""),
                ("user", """Analyze the following information and generate a code fix as a diff.

**1. Log Analyzer Report:**
```markdown
{log_analyzer_output}
```

**2. Workflow YAML Content (`{workflow_yaml_path}`):**
```yaml
{workflow_yaml_content}
```

**3. Relevant Code Snippets (from RAG search):**
{rag_snippets_formatted}

Reminder: Your output must be ONLY the diff block(s) or the string `NO_CODE_FIX_POSSIBLE`.""")
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
        Analyzes the error report, workflow YAML, and relevant code snippets to propose a fix.
        Args:
            log_analyzer_output (str): The structured Markdown output from LogAnalyzer.
            relevant_code_snippets (List[Dict[str, Any]]): Top k relevant code snippets from RAG.
            workflow_yaml_content (str): The content of the workflow YAML file associated with the failed run.
            workflow_yaml_path (str): The repository-relative path of the workflow YAML file.
            target_branch (str): The branch on which the failure occurred.
        Returns:
            str: A proposed code fix in diff format or "NO_CODE_FIX_POSSIBLE".
        """
        print("\n--- CodeFixer: Propose Fix --- ")
        # The detailed prints previously here are now part of the prompt construction

        if not self.chain:
            return "CodeFixer LLM chain not initialized. Cannot propose fix."

        rag_snippets_formatted = "No relevant code snippets provided."
        if relevant_code_snippets:
            formatted_list = []
            for i, snippet in enumerate(relevant_code_snippets):
                snippet_path = snippet.get('file_path', 'Unknown file')
                content = snippet.get('chunk_content', '')
                similarity = snippet.get('similarity', 0.0)
                formatted_list.append(
                    f"Snippet {i + 1}: File: `{snippet_path}` (Similarity: {similarity:.4f})\n"
                    f"```\n{content}\n```"
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

        print(f"CodeFixer: Invoking LLM for branch '{target_branch}'.")
        # For debugging the exact input to the LLM if needed:
        # print("--- CodeFixer Prompt Input ---")
        # print(self.prompt_template.format_prompt(**prompt_inputs).to_string())
        # print("--- End CodeFixer Prompt Input ---")

        try:
            response = await self.chain.ainvoke(prompt_inputs)
            print("--- CodeFixer LLM Raw Response ---")
            print(response)  # Print the raw response for now
            print("--- End CodeFixer LLM Raw Response ---")

            # Try to extract the last valid diff block
            # A diff block is ```diff\n...\n```
            # We look for all such blocks and take the last one that seems valid.
            diff_blocks = re.findall(r"```diff\n(.*?)\n```", response, re.DOTALL)

            if not diff_blocks:
                if response.strip() == "NO_CODE_FIX_POSSIBLE":
                    return response.strip()
                print(f"CodeFixer Warning: LLM response did not contain any ```diff ... ``` blocks.")
                return f"# LLM_RESPONSE_NO_DIFF_BLOCKS\n{response}"  # Return raw if no blocks

            # Iterate from the last found block to the first
            for block_content in reversed(diff_blocks):
                # A minimal check for a valid diff content
                if "--- a/" in block_content and "+++ b/" in block_content:
                    # Reconstruct the block with the markers
                    extracted_diff = f"```diff\n{block_content.strip()}\n```"
                    print(f"CodeFixer: Extracted the following diff block:\n{extracted_diff}")
                    return extracted_diff

            # If no valid diff block was found among the candidates
            if response.strip() == "NO_CODE_FIX_POSSIBLE":  # Check again in case it was outside blocks
                return response.strip()

            print(f"CodeFixer Warning: Found diff blocks, but none seemed valid (missing '--- a/' or '+++ b/').")
            return f"# LLM_RESPONSE_INVALID_DIFF_BLOCKS\n{response}"  # Or return the last block found, or raw response

        except Exception as e:
            print(f"Error during CodeFixer LLM invocation: {e}")
            import traceback
            traceback.print_exc()
            return "Error during CodeFixer LLM analysis."
        finally:
            print("\n--- End CodeFixer --- \n")
