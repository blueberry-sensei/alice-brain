# Tool reference

Every tool returns MCP text content. An empty state returns a placeholder string (such as "(no related material)" or "(document not found)")
rather than raising - branch on the leading text.

## list_sources()
List the sources you can currently reach: `- name (source_id=...) - N documents - M chunks`. A whole-library connection should call this first to establish the scope.

## search(query: str, top_k: int = 8, source_id: str = "")
Semantic search. Returns numbered evidence blocks: `[n] heading (chunk_id=...)\ncontent`.
It needs an LLM configured on the server (offline or unconfigured, it returns a structured error explanation).

## list_documents(source_id: str = "")
`- filename - id=<document_id> - <status> - N chunks` (only a ready status is searchable).

## outline(document_id: str)
The chunk outline ordered by rank: `rank. heading (chunk_id=...)`. While a document is processing it returns a placeholder.

## grep(pattern: str, limit: int = 20, source_id: str = "")
A LIKE exact match (case insensitive, with % and _ escaped). Returns `[n] heading (chunk_id)\n+/-240 characters of context`.

## get_chunk(chunk_id: str, source_id: str = "")
The full raw text of a chunk (heading + body). chunk_id comes from search/outline/grep.

## read(document_id: str, offset: int = 1, limit: int = 120)
The raw file paged by line (line number + content, limit <= 500). The first line states `lines a-b / N total` so paging is easy.

## get_entity(name: str, source_id: str = "")
The related event context of an entity (person/organisation/concept); an exact name match first, then a substring match.
