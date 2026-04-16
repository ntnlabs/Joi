# Memory Scaling Ideas

*Sourced from Gemini CLI architectural review, 2026-04-10*

## A. Semantic Search Performance

**Issue:** Current cosine similarity is computed in Python by iterating over all stored embeddings in `store.py`.

**Options:**
- Integrate `sqlite-vss` extension for vector search directly within SQLite.
- Alternatively, offload knowledge chunks to a lightweight local ChromaDB or LanceDB instance while keeping metadata in SQLCipher.

## B. Semantic Fact Retrieval

**Issue:** Facts are currently retrieved primarily via FTS5 (keyword-based).

**Idea:** Add a semantic layer to `user_facts`. This allows Joi to remember "I like dark roast" even when the user asks for "my favorite coffee preferences."

## C. Automated Memory Compaction

**Issue:** Large conversations may eventually overwhelm the context window even with current summarization.

**Idea:** Implement a hierarchical summarization layer (daily → weekly → permanent facts) to maintain a lean long-term memory structure.
