"""
rag_tool.py — Local document search for AgentBot.

Uses ChromaDB for storage and sentence-transformers for embeddings.
Adapted from general RAG patterns (not copied from Odysseus).
"""

import os
import json
import glob
import chromadb
from sentence_transformers import SentenceTransformer
from pypdf import PdfReader

# Where the vector database lives
DB_PATH = os.path.join(
    os.environ.get("APPDATA", "."),
    "AgentBot",
    "rag_db"
)

# Which embedding model to use. Small and fast.
MODEL_NAME = "all-MiniLM-L6-v2"

STATE_FILE = os.path.join(
    os.environ.get("APPDATA", "."), "AgentBot", "rag_state.json"
)

# Root of the flattened Telegram export. Kept in sync with
# telegram_export_parser.OUTPUT_DIR; consumers use it to scope searches to the
# group chat rather than every indexed folder.
GROUP_EXPORT_ROOT = r"C:\Users\USER\PrivateExport"


def _load_state() -> dict:
    import json
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    import json
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[rag] could not save state: {e}")


def _file_signature(path: str):
    """Return (mtime, size) or None if file is gone."""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None
    
class RAGTool:
    def __init__(self):
        os.makedirs(DB_PATH, exist_ok=True)
        self.client = chromadb.PersistentClient(path=DB_PATH)
        self.collection = self.client.get_or_create_collection(
            name="documents",
            metadata={"hnsw:space": "cosine"}
        )
        self.model = SentenceTransformer(MODEL_NAME)

    # ----------------------------------------------------------
    # INDEXING
    # ----------------------------------------------------------
    def index_file(self, filepath: str, chunk_size: int = None,
                   overlap: int = None) -> str:
        """Read one file, chunk it, embed it, store in ChromaDB.

        chunk_size/overlap default to None so callers can pass explicit
        values; when omitted they are chosen from the filepath.
        """
        ext = os.path.splitext(filepath)[1].lower()

        if ext == ".pdf":
            text = self._read_pdf(filepath)
            if not text or len(text.strip()) < 50:
                return f"Skipped {filepath} (scanned PDF or no text layer)"
        elif ext == ".docx":
            try:
                text = self._read_docx(filepath)
            except Exception as e:
                return f"Skipped {filepath} (docx read failed: {e})"
        elif ext == ".xlsx":
            try:
                text = self._read_xlsx(filepath)
            except Exception as e:
                return f"Skipped {filepath} (xlsx read failed: {e})"
        elif ext in (
            ".txt", ".md", ".py", ".json", ".log",
            ".php", ".js", ".ts", ".jsx", ".tsx",
            ".html", ".css", ".scss",
            ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".sh", ".bat", ".ps1", ".sql", ".java", ".c", ".cpp", ".go", ".rs",
        ):
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except Exception as e:
                return f"Skipped {filepath} (read failed: {e})"
        else:
            return f"Skipped {filepath} (unsupported type)"

        if not text or not text.strip():
            return f"Skipped {filepath} (empty)"

        # Skip huge files
        if len(text) > 500_000:
            return f"Skipped {filepath} (too large: {len(text)} chars)"

        # Chat logs need fine-grained chunks: a 1000-char window spans 8-15
        # messages, so a chunk's date/author blurs across several messages.
        use_small = "PrivateExport" in filepath
        if chunk_size is None:
            chunk_size = 300 if use_small else 1000
        if overlap is None:
            overlap = 50 if use_small else 200

        chunks = self._chunk(text, size=chunk_size, overlap=overlap)
        if not chunks:
            return f"No text chunks from {filepath}"

        ids = [f"{filepath}::{i}" for i in range(len(chunks))]
        embeddings = self.model.encode(chunks, show_progress_bar=False).tolist()

        try:
            self.collection.delete(where={"source": filepath})
        except Exception:
            pass

        self.collection.upsert(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=[{"source": filepath} for _ in chunks]
        )
        return f"Indexed {len(chunks)} chunks from {filepath}"

    def index_folder(self, folder: str, chunk_size: int = None,
                     overlap: int = None) -> str:
        """Index new/changed files only. Delete chunks for removed files."""
        SUPPORTED = {
            ".pdf", ".txt", ".md", ".py", ".json", ".log",
            ".php", ".js", ".ts", ".jsx", ".tsx",
            ".html", ".css", ".scss",
            ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".sh", ".bat", ".ps1", ".sql", ".java", ".c", ".cpp", ".go", ".rs",
            ".docx", ".xlsx",
        }

        SKIP_DIRS = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "env", "dist", "build", ".next", ".cache", "vendor",
        }

        SKIP_NAMES = {
            "api key.txt", "password.txt", "secrets.txt",
            "github-recovery-codes.txt",
        }

        SKIP_TOKENS = (
            "api_key", "apikey", "secret", "password",
            "token", "recovery", "credential",
        )

        state = _load_state()
        seen_paths = set()
        indexed, skipped, removed, failed = [], [], [], []

        # ---- Pass 1: walk and index changed/new files ----
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in SUPPORTED:
                    continue
                if name.lower() in SKIP_NAMES:
                    continue
                if any(tok in name.lower() for tok in SKIP_TOKENS):
                    continue

                path = os.path.join(root, name)
                seen_paths.add(path)

                sig = _file_signature(path)
                if sig is None:
                    continue

                prev = state.get(path)
                if prev and prev.get("mtime") == sig[0] and prev.get("size") == sig[1]:
                    # File unchanged since last index — skip
                    skipped.append(path)
                    continue

                print(f"  [new/changed] {path}", flush=True)
                try:
                    result = self.index_file(path, chunk_size=chunk_size,
                                             overlap=overlap)
                    if result.startswith("Indexed"):
                        state[path] = {"mtime": sig[0], "size": sig[1]}
                        indexed.append(path)
                    else:
                        failed.append(f"{path}: {result}")
                except Exception as e:
                    failed.append(f"{path}: {e}")

        # ---- Pass 2: remove DB entries for files that no longer exist ----
        for path in list(state.keys()):
            if path in seen_paths:
                continue
            # Only touch paths that were inside the folder we just walked
            if not os.path.abspath(path).startswith(os.path.abspath(folder)):
                continue
            try:
                self.collection.delete(where={"source": path})
                removed.append(path)
            except Exception:
                pass
            state.pop(path, None)

        _save_state(state)

        # ---- Build summary ----
        summary = (
            f"Indexed: {len(indexed)} new/changed\n"
            f"Skipped (unchanged): {len(skipped)}\n"
            f"Removed (deleted): {len(removed)}\n"
            f"Failed: {len(failed)}"
        )
        if indexed:
            summary += "\n\nNew/changed:\n" + "\n".join(
                f"  + {os.path.basename(p)}" for p in indexed[:10]
            )
            if len(indexed) > 10:
                summary += f"\n  ...and {len(indexed) - 10} more"
        if removed:
            summary += "\n\nRemoved:\n" + "\n".join(
                f"  - {os.path.basename(p)}" for p in removed[:10]
            )
        if failed:
            summary += "\n\nFailed:\n" + "\n".join(f"  ! {f}" for f in failed[:5])

        return summary

    # ----------------------------------------------------------
    # SEARCH
    # ----------------------------------------------------------
    def search(self, query: str, top_k: int = 8) -> str:
        """Find the most relevant chunks for a question."""
        if self.collection.count() == 0:
            return "No documents indexed yet."

        query_embedding = self.model.encode([query], show_progress_bar=False).tolist()
        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=top_k
        )

        if not results["documents"] or not results["documents"][0]:
            return "No relevant information found."

        output_parts = []
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            output_parts.append(
                f"[From: {meta.get('source', 'unknown')}]\n{doc}"
            )
        return "\n\n---\n\n".join(output_parts)

    # ----------------------------------------------------------
    # HELPERS
    # ----------------------------------------------------------
    def _read_pdf(self, path: str) -> str:
        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
                parts.append(t)
            except Exception:
                continue
        return "\n".join(parts)

    def _read_docx(self, path: str) -> str:
        from docx import Document
        doc = Document(path)
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)

    def _read_xlsx(self, path: str) -> str:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            parts.append(f"--- Sheet: {sheet.title} ---")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(" | ".join(cells))
        wb.close()
        return "\n".join(parts)

    def _chunk(self, text: str, size: int = 1000, overlap: int = 200):
        """Split text into overlapping chunks."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + size
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - overlap
        return chunks


# ----------------------------------------------------------
# Singleton
# ----------------------------------------------------------
_rag = None

def get_rag():
    global _rag
    if _rag is None:
        _rag = RAGTool()
    return _rag


def index_documents(path: str) -> str:
    """Public function AgentBot calls."""
    rag = get_rag()
    if os.path.isdir(path):
        return rag.index_folder(path)
    return rag.index_file(path)


def search_documents(query: str) -> str:
    """Public function AgentBot calls."""
    return get_rag().search(query)