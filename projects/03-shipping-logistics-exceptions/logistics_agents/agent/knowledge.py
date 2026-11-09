"""RAG: fetch -> inject -> generate. Indexes knowledge/*.md (synthetic policies, contracts, guides)."""
import pathlib

from agentcore import KeywordRetriever, Retriever

KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parents[2] / "knowledge"


def get_retriever() -> Retriever:
    """TODO(RAG milestone): swap for a pgvector retriever with embeddings; keep citations (source#chunk)."""
    kb = KeywordRetriever()
    kb.add_dir(KNOWLEDGE_DIR)
    return kb
