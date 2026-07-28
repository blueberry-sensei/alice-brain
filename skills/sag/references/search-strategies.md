# Query strategy

## The funnel (saves tokens, locates precisely)
1. `list_sources` -> confirm the reachable scope and obtain source_id
2. `list_documents(source_id)` -> narrow to the candidate documents
3. `outline(doc)` -> find the chunk_id of the target section
4. `search` (semantic) or `grep` (exact) to recall the evidence
5. `get_chunk(chunk_id)` or a paged `read` -> take only the raw text you need

## When to use search versus grep
- **search**: questions, concepts, vague phrasing ("what does the expense approval chain look like")
- **grep**: a definite string - an identifier (INV-2024), a function name, the exact occurrence of a proper noun

## Citation discipline
Mark every fact in the answer with `[n]` (the number from the search result); when asked for provenance, show the raw text with get_chunk.

## Reading a large file page by page
`read` starts at offset=1, and the returned "N lines total" tells you how to page; never read a whole large file at once.
