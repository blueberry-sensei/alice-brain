---
name: sag-knowledge
description: Use when an AI coding agent needs to search, browse, cite, or read documents from a SAG knowledge base through MCP.
---

# SAG Knowledge Base

SAG turns your documents into a searchable, traceable knowledge base and exposes it to any agent over **MCP**.
This skill teaches an agent to use SAG's 8 read-only tools through the exploration funnel: confirm the scope, then look at the structure, then fetch the exact content.

## Connecting

Copy the whole-library MCP configuration from Settings -> Integrations in the SAG interface. To narrow it to one source, append `?source_id=<SOURCE_ID>` to the HTTP URL, or:

```bash
curl -s http://<host>/api/v1/sources/<SOURCE_ID>/mcp -H "Authorization: Bearer <TOKEN>"
```

- **HTTP (recommended)**: `http://<host>/mcp/?source_id=<SOURCE_ID>`, with the header `Authorization: Bearer <TOKEN>`
- **stdio**: `SAG_MCP_SOURCE_ID=<SOURCE_ID> python -m sag_api.mcp.server` (needs the apps/api environment)

## Tools and how to use them (in funnel order)

| Order | Tool | When to use it |
| --- | --- | --- |
| 1 | `list_sources()` | See the knowledge sources you can reach with their document and chunk counts, and obtain source_id |
| 2 | `list_documents(source_id?)` | Learn which documents are in scope (id/status/chunk count) |
| 3 | `outline(document_id)` | See a document's outline (heading order + chunk_id) and locate the section |
| 4 | `search(query, top_k?, source_id?)` | Semantic recall: a natural-language question -> numbered evidence (chunk_id included) |
| 5 | `grep(pattern, limit?, source_id?)` | Exact match: a proper noun, an identifier or a code fragment (case insensitive) |
| 6 | `get_chunk(chunk_id, source_id?)` | Read one chunk's full raw text (the end point of citation provenance) |
| 7 | `read(document_id, offset?, limit?)` | Read the raw file page by page (120 lines per page by default) |
| 8 | `get_entity(name, source_id?)` | Clarify a person or concept: the related event context of an entity |

**Principle**: on a whole-library connection, use `list_sources` first to establish the scope. Cite the `[n]` numbers `search` returns; when unsure, narrow with `outline`/`grep` before reading,
so a whole-document `read` does not waste the context. See references/ for the details.

## References

- [references/mcp-tools.md](references/mcp-tools.md) - each tool's parameters and return shape
- [references/search-strategies.md](references/search-strategies.md) - the query strategy and the funnel
