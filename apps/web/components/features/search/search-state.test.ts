import { describe, expect, it } from "vitest";

import type { SearchResponse } from "@/lib/types";
import {
  appendSearchSummary,
  beginSearchLifecycle,
  canLoadMoreSearch,
  completeSearchLifecycle,
  createSearchKey,
  DEFAULT_SEARCH_TOP_K,
  failSearchLifecycle,
  receiveSearchResult,
  resolveSearchRun,
  type SearchLifecycleState,
} from "./search-state";

function response(query: string, summary = "old answer", sectionCount = 1): SearchResponse {
  return {
    query,
    sections: Array.from({ length: sectionCount }, (_, index) => ({
      chunk_id: `chunk-${index}`,
      heading: `Evidence ${index + 1}`,
      content: "content",
      score: 1,
      rank: index + 1,
      source_id: "source-a",
      source_name: "Source A",
    })),
    events: [],
    entities: [],
    relations: [],
    source_hits: [],
    summary,
    exploration_id: null,
    stats: {},
  };
}

function readyState(): SearchLifecycleState {
  const key = createSearchKey({
    query: "old question",
    strategy: "vector",
    sourceIds: ["source-a"],
  });
  return {
    result: response("old question"),
    busy: false,
    phase: "idle",
    summaryStreaming: false,
    error: "",
    committedSearchKey: key,
    lastQuery: "old question",
    lastStrategy: "vector",
    topK: 12,
    hasMore: true,
  };
}

describe("search identity and run intent", () => {
  it("normalizes outer query whitespace and source order", () => {
    expect(createSearchKey({
      query: "  question  ",
      strategy: "vector",
      sourceIds: ["b", "a", "a"],
    })).toBe(createSearchKey({
      query: "question",
      strategy: "vector",
      sourceIds: ["a", "b"],
    }));
  });

  it("defaults a replacement search to 12 even after a larger committed search", () => {
    const run = resolveSearchRun({
      requestedKey: "new",
      committedKey: "old",
      committedTopK: 48,
      hasResult: true,
      idle: true,
    });
    expect(run).toEqual({ intent: "replace", topK: DEFAULT_SEARCH_TOP_K });
  });

  it("retains results only for an idle same-key top-k increase", () => {
    const run = resolveSearchRun({
      intent: "load-more",
      requestedKey: "same",
      requestedTopK: 24,
      committedKey: "same",
      committedTopK: 12,
      hasResult: true,
      idle: true,
    });
    expect(run).toEqual({ intent: "load-more", topK: 24 });
  });

  it.each([
    { label: "different identity", requestedKey: "new", requestedTopK: 24, idle: true },
    { label: "non-increasing top-k", requestedKey: "same", requestedTopK: 12, idle: true },
    { label: "active request", requestedKey: "same", requestedTopK: 24, idle: false },
  ])("downgrades ambiguous load-more: $label", ({ requestedKey, requestedTopK, idle }) => {
    const run = resolveSearchRun({
      intent: "load-more",
      requestedKey,
      requestedTopK,
      committedKey: "same",
      committedTopK: 12,
      hasResult: true,
      idle,
    });
    expect(run).toEqual({ intent: "replace", topK: DEFAULT_SEARCH_TOP_K });
  });

  it("allows more only while the idle draft still identifies the committed result", () => {
    expect(canLoadMoreSearch({
      phase: "idle",
      hasResult: true,
      hasMore: true,
      topK: 12,
      committedKey: "same",
      draftKey: "same",
    })).toBe(true);
    expect(canLoadMoreSearch({
      phase: "idle",
      hasResult: true,
      hasMore: true,
      topK: 12,
      committedKey: "same",
      draftKey: "edited-query",
    })).toBe(false);
  });
});

describe("search lifecycle", () => {
  it("clears stale results immediately for a replacement search", () => {
    const next = beginSearchLifecycle(readyState(), {
      intent: "replace",
      topK: 12,
      strategy: "multi",
    });
    expect(next).toMatchObject({
      result: null,
      busy: true,
      phase: "searching",
      summaryStreaming: false,
      committedSearchKey: "",
      lastQuery: "",
      hasMore: false,
    });
  });

  it("preserves committed results while load-more retrieves", () => {
    const current = readyState();
    const next = beginSearchLifecycle(current, {
      intent: "load-more",
      topK: 24,
      strategy: "vector",
    });
    expect(next.result).toBe(current.result);
    expect(next).toMatchObject({ busy: true, phase: "loading-more", topK: 12 });
  });

  it("streams deltas and commits the authoritative completed result", () => {
    const current = beginSearchLifecycle(readyState(), {
      intent: "replace",
      topK: 12,
      strategy: "vector",
    });
    const base = response("new question", "");
    const streaming = appendSearchSummary(
      appendSearchSummary(receiveSearchResult(current, base), "part one"),
      "part two",
    );
    expect(streaming).toMatchObject({
      busy: true,
      phase: "streaming",
      summaryStreaming: true,
    });
    expect(streaming.result?.summary).toBe("part onepart two");

    const completed = completeSearchLifecycle(streaming, response("new question", "final answer"), {
      key: "new-key",
      query: "new question",
      strategy: "vector",
      topK: 12,
      hasMore: false,
    });
    expect(completed).toMatchObject({
      busy: false,
      phase: "idle",
      summaryStreaming: false,
      committedSearchKey: "new-key",
      lastQuery: "new question",
      topK: 12,
      hasMore: false,
    });
    expect(completed.result?.summary).toBe("final answer");
  });

  it("rolls a failed load-more back to the complete committed snapshot", () => {
    const committed = readyState();
    const loading = beginSearchLifecycle(committed, {
      intent: "load-more",
      topK: 24,
      strategy: "vector",
    });
    const partial = appendSearchSummary(
      receiveSearchResult(loading, response("old question", "", 2)),
      "unfinished",
    );
    const rolledBack = failSearchLifecycle(partial, "connection interrupted", committed);
    expect(rolledBack.result).toBe(committed.result);
    expect(rolledBack).toMatchObject({
      phase: "idle",
      busy: false,
      summaryStreaming: false,
      topK: 12,
      hasMore: true,
      error: "connection interrupted",
    });
  });

  it("does not restore the previous query when a replacement fails", () => {
    const loading = receiveSearchResult(
      beginSearchLifecycle(readyState(), {
        intent: "replace",
        topK: 12,
        strategy: "vector",
      }),
      response("new question", ""),
    );
    const failed = failSearchLifecycle(loading, "search failed", null);
    expect(failed).toMatchObject({
      result: null,
      phase: "idle",
      busy: false,
      lastQuery: "",
      committedSearchKey: "",
      error: "search failed",
    });
  });
});
