"""
embedder.py — RAG layer for Casey

How it works:
  1. Each vendor/ticket is converted to a plain-text sentence.
  2. SentenceTransformer encodes it into a 384-dimensional vector (a list
     of floats that captures semantic meaning).
  3. ChromaDB stores those vectors locally on disk.
  4. At query time, the user's question is embedded the same way, and
     ChromaDB finds the closest vectors using cosine similarity.
  5. Only those top-K documents are passed to Groq — not everything.
"""

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# all-MiniLM-L6-v2: lightweight (22M params, ~90MB download on first run),
# fast on CPU, and great at semantic similarity tasks.
EMBED_MODEL = "all-MiniLM-L6-v2"

ef = SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)

# PersistentClient saves vectors to disk so they survive restarts.
client = chromadb.PersistentClient(path="./chroma_db")
collection = client.get_or_create_collection(
    name="casey_knowledge",
    embedding_function=ef,
    metadata={"hnsw:space": "cosine"},  # use cosine similarity
)


def index_data(vendors: list[dict], tickets: list[dict]) -> None:
    """
    Convert vendor and ticket dicts to text documents, embed them,
    and store in ChromaDB. Clears stale data before re-indexing.
    """
    docs, ids, metadatas = [], [], []

    for i, v in enumerate(vendors):
        text = (
            f"Vendor: {v['name']}. "
            f"Contract Status: {v['status']}. "
            f"Deliverable: {v['deliverable']}. "
            f"Due Date: {v['due_date']}. "
            f"Notes: {v['notes']}."
        )
        docs.append(text)
        ids.append(f"vendor_{i}")
        metadatas.append({"type": "vendor", "name": v["name"]})

    for i, t in enumerate(tickets):
        text = (
            f"Jira Ticket {t['key']}: {t['summary']}. "
            f"Status: {t['status']}. "
            f"Priority: {t['priority']}."
        )
        docs.append(text)
        ids.append(f"ticket_{i}")
        metadatas.append({"type": "ticket", "key": t["key"]})

    # Wipe old vectors so re-indexing stays fresh
    existing = collection.get()
    if existing["ids"]:
        collection.delete(ids=existing["ids"])

    if docs:
        collection.add(documents=docs, ids=ids, metadatas=metadatas)
        print(
            f"[embedder] Indexed {len(docs)} docs "
            f"({len(vendors)} vendors, {len(tickets)} tickets)"
        )


def search(query: str, top_k: int = 10) -> list[str]:
    """
    Embed the query and return the top-K most semantically similar
    documents from ChromaDB.
    """
    count = collection.count()
    if count == 0:
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(top_k, count),
    )
    return results["documents"][0] if results["documents"] else []
