import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.prompts import ChatPromptTemplate
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

class LogAnalyzer:
    def __init__(self, model_name="gemini-1.5-flash-latest", temperature=0.2):
        """
        Initializes the LogAnalyzer agent.
        Args:
            model_name (str): The name of the Gemini model to use.
            temperature (float): The temperature for the LLM's generation.
                                 Lower values make the output more deterministic.
        """
        try:
            self.llm = ChatGoogleGenerativeAI(model=model_name, temperature=temperature)
        except Exception as e:
            print(f"Error initializing ChatGoogleGenerativeAI: {e}")
            print("Please ensure your GOOGLE_API_KEY is correctly set in the .env file and valid.")
            raise

        # Detailed prompt for log analysis
        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system", ("""You are an expert log analysis agent. Your primary task is to analyze the provided logs from a failed software deployment or CI/CD workflow.
You must perform the following actions:
1.  Carefully examine all provided log files to identify error messages, failure indicators, exceptions, and any anomalous behavior.
2.  Determine the most likely primary root cause of the failure. If there are multiple contributing factors, explain their relationship.
3.  Provide a detailed, technical, step-by-step analysis of what went wrong. Reference specific log files or step names if they are discernible from the log content (e.g., 'In 03_deploy.txt, the connection timed out...').
4.  Extract and quote the **exact log lines** (verbatim) that are most critical for understanding the error and its immediate context. Ensure these quotes are clearly demarcated.
5.  If possible, based strictly on the log information, suggest general areas to investigate or common types of fixes for the identified root cause (e.g., 'Check network connectivity to the deployment server,' or 'Verify database credentials'). Do not invent solutions not supported by the logs.
Present your final analysis in a clear, structured, and actionable format.""")),
            ("user", ("""Please analyze the following workflow logs:

```text
{log_content}
```

Detailed Analysis:""")),
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
                all_log_contents.append(f"--- Log File: {log_file_path.name} ---\n{content}\n--- End Log File: {log_file_path.name} ---")
            except Exception as e:
                all_log_contents.append(f"--- Error reading log file: {log_file_path.name} ---\n{str(e)}\n--- End Error ---")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000 
        if len(combined_logs) > max_chars:
            print(f"Warning: Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
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
                all_log_contents.append(f"--- Log File: {log_file_path.name} ---\n{content}\n--- End Log File: {log_file_path.name} ---")
            except Exception as e:
                all_log_contents.append(f"--- Error reading log file: {log_file_path.name} ---\n{str(e)}\n--- End Error ---")

        combined_logs = "\n\n".join(all_log_contents)

        max_chars = 1000000
        if len(combined_logs) > max_chars:
            print(f"Warning (async): Combined log length ({len(combined_logs)} chars) exceeds truncation threshold ({max_chars} chars). Truncating.")
            combined_logs = combined_logs[:max_chars] + "\n... (logs truncated due to length)"
        
        if not combined_logs.strip():
            return "Error: All log files were empty or unreadable."

        try:
            # Use ainvoke for the asynchronous call
            response = await self.chain.ainvoke({"log_content": combined_logs})
            return response
        except Exception as e:
            return f"Error during LLM invocation (async): {e}\nMake sure your GOOGLE_API_KEY is valid and the model is accessible."
