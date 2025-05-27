import hashlib
from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
from langchain_core.documents import Document
from typing import List

def chunk_file_content(file_content: str, file_path: str, chunk_size=1000, chunk_overlap=200) -> List[Document]:
    """Chunks file content based on its type. Returns list of Langchain Documents."""
    file_extension = file_path.split('.')[-1].lower()
    
    lang_map = {
        "py": Language.PYTHON,
        "js": Language.JS,
        "ts": Language.TS,
        "md": Language.MARKDOWN,
        "java": Language.JAVA,
        "c": Language.C,
        "cpp": Language.CPP,
        "cs": Language.CSHARP,
        "go": Language.GO,
        "html": Language.HTML,
        "php": Language.PHP,
        "rb": Language.RUBY,
        "rs": Language.RUST,
        "scala": Language.SCALA,
        "swift": Language.SWIFT,
        "tex": Language.LATEX,
    }

    if file_extension in lang_map:
        splitter = RecursiveCharacterTextSplitter.from_language(
            language=lang_map[file_extension], chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    else:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    
    docs = splitter.create_documents([file_content], metadatas=[{"source": file_path}])
    return docs

def calculate_file_hash(file_content: str) -> str:
    """Calculates SHA256 hash of file content."""
    return hashlib.sha256(file_content.encode('utf-8')).hexdigest() 