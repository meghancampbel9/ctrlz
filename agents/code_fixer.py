import os
import asyncio
import traceback # Added for error logging
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from typing import List, Dict, Any

load_dotenv()

class CodeFixer:
    def __init__(self, model_name="gemini-1.5-flash-latest", temperature=0.3):
        """
        Initializes the CodeFixer agent.
        Args:
            model_name (str): The name of the Gemini model to use.
            temperature (float): The temperature for the LLM's generation.
        """
        self.llm = None
        self.chain = None
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

    async def propose_fix(
        self,
        log_analyzer_output: str,
        relevant_code_snippets: List[Dict[str, Any]],
        workflow_yaml_content: str, 
        workflow_yaml_path: str,
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
                similarity = snippet.get('similarity', 0.0)
                formatted_list.append(
                    f"Snippet {i+1}: File: `{snippet_path}` (Similarity: {similarity:.4f})\n"
                    f"Original Content of `{snippet_path}`:\n"
                    f"```\n{content}\n```"  # RAG provides full file content
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

        try:
            response = await self.chain.ainvoke(prompt_inputs)
            print("--- CodeFixer LLM Raw Response ---")
            if len(response) > 2000:
                print(response[:1000] + "\n... (response truncated in log) ...\n" + response[-1000:])
            else:
                print(response)
            print("--- End CodeFixer LLM Raw Response ---")

            response_stripped = response.strip()
            if response_stripped == "NO_CODE_FIX_POSSIBLE":
                print("CodeFixer: LLM responded NO_CODE_FIX_POSSIBLE.")
                return response_stripped

            has_begin_end_markers = "==BEGIN FILE:" in response and "==END FILE:" in response
            has_delete_marker = "==DELETE FILE:" in response_stripped
            is_single_delete_instruction = has_delete_marker and response_stripped.startswith("==DELETE FILE:") and response_stripped.count('\n') == 0

            if has_begin_end_markers or is_single_delete_instruction:
                if has_begin_end_markers:
                    print("CodeFixer: LLM response appears to contain file content blocks.")
                if is_single_delete_instruction:
                     print("CodeFixer: LLM response appears to be a single delete file instruction.")
                return response
            elif has_delete_marker :
                 print("CodeFixer: LLM response appears to contain delete file instruction(s).")
                 return response
            else:
                print(f"CodeFixer Warning: LLM response did not conform to expected output structure (NO_CODE_FIX_POSSIBLE, BEGIN/END FILE blocks, or DELETE FILE). Response was: '{response_stripped[:500]}...'")
                return f"# LLM_RESPONSE_UNEXPECTED_FORMAT\n{response}"

        except Exception as e:
            print(f"Error during CodeFixer LLM invocation: {e}")
            traceback.print_exc()
            return "Error during CodeFixer LLM analysis."
        finally:
            print("\n--- End CodeFixer --- \n") 