"""RAG: fetch -> inject -> generate. Indexes knowledge/*.md (synthetic policies, contracts, guides)."""
import pathlib

from agentcore import KeywordRetriever

KNOWLEDGE_DIR = pathlib.Path(__file__).resolve().parents[2] / "knowledge"


def get_retriever() -> KeywordRetriever:
    """TODO(RAG milestone): swap for a pgvector retriever with embeddings; keep citations (source#chunk)."""
    kb = KeywordRetriever()
    kb.add_dir(KNOWLEDGE_DIR)
    return kb


def known_citations() -> set[str]:
    """Every valid citation id ([source.md#n]); the verifier rejects any other."""
    return {c.cite for c in get_retriever().chunks}
