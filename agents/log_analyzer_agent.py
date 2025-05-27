import os
import asyncio
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser
from typing import List, Dict, Any
from services.db.workflow_logs_service import get_workflow_logs_for_run # Corrected import

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
            workflow_name = entry.get('workflow_name', 'N/A')

            header = f"--- Log File: {log_filename} (Job: {job_name}, Workflow: {workflow_name}) ---"
            footer = f"--- End Log File: {log_filename} ---"
            all_log_contents.append(f"{header}\n{content}\n{footer}")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000
        if len(combined_logs) > max_chars:
            print(f"Warning (Supabase logs): Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
            combined_logs = combined_logs[:max_chars] + "\n... (logs truncated due to length)"

        if not combined_logs.strip():
            return "Error: All log files from Supabase were empty or unreadable."

        try:
            response = await self.chain.ainvoke({"log_content": combined_logs})
            return response
        except Exception as e_llm:
            return f"Error during LLM invocation with Supabase logs: {e_llm}\nMake sure your GOOGLE_API_KEY is valid and the model is accessible." 