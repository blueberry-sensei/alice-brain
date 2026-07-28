import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentEvent, AgentEventType, AgentRunOutcome } from "./sse";
import type { Message, MessagePage, UniverseActivation } from "./types";
import {
  ConversationBusyError,
  ConversationRuntime,
  type ConversationTransport,
} from "./conversation-runtime";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function message(id: string, content: string, promptPreview?: string): Message {
  return {
    id,
    thread_id: "thread-1",
    role: "assistant",
    content,
    citations: [],
    attachments: [],
    steps: [],
    prompt_preview: promptPreview,
    created_at: "2026-07-12T00:00:00Z",
  };
}

function messageRange(from: number, to: number): Message[] {
  return Array.from({ length: to - from + 1 }, (_, index) => {
    const value = from + index;
    return message(`message-${value}`, `message ${value}`);
  });
}

function persistedAnswer(): MessagePage {
  return page([
    {
      id: "server-user",
      thread_id: "thread-1",
      role: "user",
      content: "what time is it",
      citations: [],
      attachments: [],
      steps: [],
      created_at: "2026-07-12T00:00:00Z",
    },
    {
      id: "server-assistant",
      thread_id: "thread-1",
      role: "assistant",
      content: "it is eight o'clock",
      citations: [],
      attachments: [],
      steps: [{ kind: "answer", step: 1, ms: 200 }],
      created_at: "2026-07-12T00:00:01Z",
    },
  ]);
}

function page(items: Message[] = []): MessagePage {
  return { items, has_more: false, next_cursor: null };
}

function event(
  type: AgentEventType,
  sequence: number,
  payload: Record<string, unknown> = {},
  turn = 1,
  runId = "run-1",
): AgentEvent {
  return {
    version: 1,
    type,
    run_id: runId,
    sequence,
    timestamp: "2026-07-12T00:00:00Z",
    turn,
    payload,
  };
}

class FakeTransport implements ConversationTransport {
  readonly streamResult = deferred<AgentRunOutcome>();
  readonly createCalls: string[] = [];
  readonly cancelCalls: string[] = [];
  readonly approvalCalls: string[] = [];
  readonly deleteCalls: string[] = [];
  readonly historyPages: MessagePage[] = [];
  readonly historyCursors: Array<string | null | undefined> = [];
  readonly streamCalls: Array<{ webEnabled: boolean; knowledgeOnly?: boolean }> = [];
  approvalResult: Promise<void> | null = null;
  onEvent: ((event: AgentEvent) => void) | null = null;
  streamSignal: AbortSignal | null = null;
  createdThreadId = "thread-created";

  async createThread(input: { title: string }): Promise<{ id: string }> {
    this.createCalls.push(input.title);
    return { id: this.createdThreadId };
  }

  async listMessages(input?: { cursor?: string | null }): Promise<MessagePage> {
    this.historyCursors.push(input?.cursor);
    return this.historyPages.shift() ?? page();
  }

  stream(input: Parameters<ConversationTransport["stream"]>[0]): Promise<AgentRunOutcome> {
    this.streamCalls.push({
      webEnabled: input.webEnabled,
      knowledgeOnly: input.knowledgeOnly,
    });
    this.onEvent = input.onEvent;
    this.streamSignal = input.signal;
    return this.streamResult.promise;
  }

  async cancelRun(input: { runId: string }): Promise<void> {
    this.cancelCalls.push(input.runId);
  }

  async approveTool(input: { toolCallId: string }): Promise<void> {
    this.approvalCalls.push(`approve:${input.toolCallId}`);
    if (this.approvalResult) await this.approvalResult;
  }

  async rejectTool(input: { toolCallId: string }): Promise<void> {
    this.approvalCalls.push(`reject:${input.toolCallId}`);
  }

  async deleteMessage(input: { messageId: string }): Promise<void> {
    this.deleteCalls.push(input.messageId);
  }

  emit(value: AgentEvent): void {
    if (!this.onEvent) throw new Error("stream has not started");
    this.onEvent(value);
  }
}

function runtime(
  transport: ConversationTransport,
  options: {
    onUniverseActivation?: (activation: UniverseActivation) => void;
    maxSessions?: number;
    maxMessagesPerSession?: number;
  } = {},
) {
  let serial = 0;
  return new ConversationRuntime({
    agentId: "agent-1",
    transport,
    flushIntervalMs: 50,
    stopGraceMs: 100,
    now: () => 1_000,
    createId: (prefix) => `${prefix}-${++serial}`,
    ...options,
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("conversation runtime", () => {
  it("keeps stable session/thread identity and loads MessagePage history", async () => {
    const transport = new FakeTransport();
    transport.historyPages.push(
      page([message("message-1", "historical answer", "[System instructions]\nrules\n\n[Current question]\nquestion")]),
    );
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });

    expect(conversations.forThread("thread-1")).toBe(sessionId);
    expect(conversations.getIndexSnapshot().activeSessionId).toBe(sessionId);

    await conversations.ensureHistory(sessionId);
    expect(conversations.getSessionSnapshot(sessionId)).toMatchObject({
      threadId: "thread-1",
      history: { status: "ready", hasMore: false, nextCursor: null },
      messages: [
        {
          id: "message-1",
          content: "historical answer",
          delivery: "persisted",
          promptPreview: "[System instructions]\nrules\n\n[Current question]\nquestion",
        },
      ],
    });
  });

  it("normalizes persisted external citations without treating them as knowledge chunks", async () => {
    const transport = new FakeTransport();
    const persisted = message("message-1", "a historical answer with an external source");
    persisted.citations = [
      {
        n: 1,
        kind: "external",
        url: "https://news.example.com/story",
        title: "External coverage",
        source: "Example News",
        mapped: false,
        claim_level: "run",
      } as unknown as Message["citations"][number],
    ];
    transport.historyPages.push(page([persisted]));
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });

    await conversations.ensureHistory(sessionId);

    expect(conversations.getSessionSnapshot(sessionId).messages[0].citations).toEqual([
      {
        n: 1,
        kind: "external",
        chunk_id: null,
        heading: "External coverage",
        snippet: "",
        score: 0,
        source_id: null,
        source_name: "Example News",
        url: "https://news.example.com/story",
        title: "External coverage",
        source: "Example News",
        mapped: false,
        claim_level: "run",
      },
    ]);
  });

  it("prepends older history by cursor and de-duplicates overlapping ids", async () => {
    const transport = new FakeTransport();
    transport.historyPages.push(
      {
        items: [message("message-2", "the second"), message("message-3", "the third")],
        has_more: true,
        next_cursor: "older-1",
      },
      {
        items: [message("message-1", "the first"), message("message-2", "the second again")],
        has_more: false,
        next_cursor: null,
      },
    );
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });

    await conversations.ensureHistory(sessionId);
    await conversations.loadOlder(sessionId);

    expect(transport.historyCursors).toEqual([null, "older-1"]);
    expect(
      conversations.getSessionSnapshot(sessionId).messages.map((item) => item.id),
    ).toEqual(["message-1", "message-2", "message-3"]);
    expect(conversations.getSessionSnapshot(sessionId).history).toMatchObject({
      status: "ready",
      hasMore: false,
      nextCursor: null,
    });
  });

  it("stops older pagination at the per-session message window", async () => {
    const transport = new FakeTransport();
    transport.historyPages.push(
      {
        items: messageRange(81, 120),
        has_more: true,
        next_cursor: "older-1",
      },
      {
        items: messageRange(41, 80),
        has_more: true,
        next_cursor: "older-2",
      },
      {
        items: messageRange(1, 40),
        has_more: true,
        next_cursor: "older-3",
      },
    );
    const conversations = runtime(transport, { maxMessagesPerSession: 100 });
    const sessionId = conversations.forThread("thread-1", { activate: true });

    await conversations.ensureHistory(sessionId);
    await conversations.loadOlder(sessionId);
    await conversations.loadOlder(sessionId);

    const snapshot = conversations.getSessionSnapshot(sessionId);
    const ids = snapshot.messages.map((item) => item.id);
    expect(ids).toHaveLength(100);
    expect(ids[0]).toBe("message-21");
    expect(ids.at(-1)).toBe("message-120");
    expect(new Set(ids)).toHaveLength(100);
    expect(snapshot.history).toMatchObject({
      status: "ready",
      hasMore: false,
      nextCursor: null,
    });

    await conversations.loadOlder(sessionId);
    expect(transport.historyCursors).toEqual([null, "older-1", "older-2"]);
  });

  it("clamps small limits to one page and protects the active user/assistant pair", async () => {
    const transport = new FakeTransport();
    transport.historyPages.push(
      {
        items: messageRange(1, 40),
        has_more: true,
        next_cursor: "older-1",
      },
      {
        items: messageRange(41, 80),
        has_more: true,
        next_cursor: "older-2",
      },
    );
    const conversations = runtime(transport, { maxMessagesPerSession: 5 });
    const sessionId = conversations.forThread("thread-1", { activate: true });
    await conversations.ensureHistory(sessionId);

    const running = conversations.send(sessionId, { query: "current question" });
    await conversations.ensureHistory(sessionId, { force: true });

    const active = conversations.getSessionSnapshot(sessionId);
    expect(active.messages).toHaveLength(40);
    expect(active.messages.filter((item) => item.delivery === "pending")).toMatchObject([
      { role: "user", content: "current question" },
    ]);
    expect(active.messages.filter((item) => item.delivery === "streaming")).toMatchObject([
      { role: "assistant" },
    ]);
    expect(new Set(active.messages.map((item) => item.id))).toHaveLength(40);

    await conversations.loadOlder(sessionId);
    expect(transport.historyCursors).toEqual([null, null]);
    expect(conversations.getSessionSnapshot(sessionId).history).toMatchObject({
      hasMore: false,
      nextCursor: null,
    });

    transport.streamResult.resolve({ status: "cancelled", runId: "run-1" });
    await running;
    const finished = conversations.getSessionSnapshot(sessionId);
    expect(finished.messages).toHaveLength(40);
    expect(finished.messages.some((item) => item.role === "user" && item.content === "current question")).toBe(true);
    expect(finished.messages.at(-1)).toMatchObject({
      role: "assistant",
      delivery: "cancelled",
    });
  });

  it("bounds the session cache with LRU while protecting active and subscribed sessions", () => {
    const transport = new FakeTransport();
    const conversations = runtime(transport, { maxSessions: 3 });
    const active = conversations.forThread("thread-active", { activate: true });
    const subscribed = conversations.forThread("thread-subscribed");
    const unsubscribe = conversations.subscribeSession(subscribed, () => {});
    const evictable = conversations.forThread("thread-evictable");

    conversations.forThread("thread-new");
    const sessionIds = conversations.getIndexSnapshot().sessions.map((item) => item.sessionId);
    expect(sessionIds).toHaveLength(3);
    expect(sessionIds).toContain(active);
    expect(sessionIds).toContain(subscribed);
    expect(sessionIds).not.toContain(evictable);

    unsubscribe();
  });

  it("has one global active run, batches tokens, and survives subscriber unmount", async () => {
    vi.useFakeTimers();
    const transport = new FakeTransport();
    const conversations = runtime(transport);
    const first = conversations.forThread("thread-1", { activate: true });
    const second = conversations.forThread("thread-2");
    const listener = vi.fn();
    const unsubscribe = conversations.subscribeSession(first, listener);

    const running = conversations.send(first, { query: "what time is it" });
    expect(conversations.getIndexSnapshot().activeRunSessionId).toBe(first);
    await expect(conversations.send(second, { query: "another question" })).rejects.toBeInstanceOf(
      ConversationBusyError,
    );

    unsubscribe();
    transport.emit(event("run.started", 0, { user_message_id: "server-user" }));
    transport.emit(event("turn.started", 1));
    transport.emit(event("message.delta", 2, { role: "assistant", delta: "it is " }));
    transport.emit(event("message.delta", 3, { role: "assistant", delta: "eight o'clock" }));
    expect(conversations.getSessionSnapshot(first).messages.at(-1)?.content).toBe("");

    await vi.advanceTimersByTimeAsync(49);
    expect(conversations.getSessionSnapshot(first).messages.at(-1)?.content).toBe("");
    await vi.advanceTimersByTimeAsync(1);
    expect(conversations.getSessionSnapshot(first).messages.at(-1)?.content).toBe("it is eight o'clock");

    transport.emit(
      event("message.completed", 4, {
        message: { role: "assistant" },
        duration_ms: 200,
        has_tool_calls: false,
      }),
    );
    transport.emit(
      event("run.completed", 5, { citations: [], prompt_preview: "unified prompt preview" }),
    );
    transport.historyPages.push(persistedAnswer());
    transport.streamResult.resolve({
      status: "completed",
      runId: "run-1",
      messageId: "server-assistant",
    });
    await running;

    expect(conversations.getSessionSnapshot(first).run).toBeNull();
    expect(conversations.getSessionSnapshot(first).messages.at(-1)).toMatchObject({
      content: "it is eight o'clock",
      delivery: "persisted",
      steps: [{ kind: "answer", ms: 200 }],
      promptPreview: "unified prompt preview",
    });
    expect(conversations.getSessionSnapshot(first).messages).toHaveLength(2);
    expect(transport.streamSignal?.aborted).toBe(false);
  });

  it("captures web access per round and always sends an explicit boolean", async () => {
    const transport = new FakeTransport();
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });

    const first = conversations.send(sessionId, {
      query: "look up the latest news",
      webEnabled: true,
    });
    expect(transport.streamCalls).toEqual([{ webEnabled: true, knowledgeOnly: undefined }]);

    transport.streamResult.resolve({ status: "completed", runId: "run-1" });
    await first;

    await conversations.send(sessionId, { query: "answer only from the knowledge base" });
    expect(transport.streamCalls).toEqual([
      { webEnabled: true, knowledgeOnly: undefined },
      { webEnabled: false, knowledgeOnly: undefined },
    ]);
  });

  it("replaces buffered stream text with the canonical terminal answer and citations", async () => {
    vi.useFakeTimers();
    const transport = new FakeTransport();
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });
    const running = conversations.send(sessionId, { query: "verify the material" });

    transport.emit(event("message.delta", 1, { delta: "unverified content [99]" }));
    transport.emit(
      event("run.completed", 2, {
        output: "verified conclusion [1]",
        citations: [
          {
            n: 1,
            chunk_id: "chunk-1",
            source_id: "source-1",
            heading: "Basis",
            snippet: "raw text",
            score: 0.9,
            event_refs: [
              {
                id: "event-1",
                title: "Real event title",
                summary: "Real event summary.",
                category: "Product news",
              },
            ],
          },
        ],
        prompt_preview: "[System instructions]\nrules\n\n[Current question]\nverify the material",
      }),
    );

    const terminal = conversations.getSessionSnapshot(sessionId).messages.at(-1);
    expect(terminal).toMatchObject({
      content: "verified conclusion [1]",
      citations: [
        {
          n: 1,
          chunk_id: "chunk-1",
          source_id: "source-1",
          event_refs: [
            {
              id: "event-1",
              title: "Real event title",
              summary: "Real event summary.",
              category: "Product news",
            },
          ],
        },
      ],
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(conversations.getSessionSnapshot(sessionId).messages.at(-1)?.content).toBe(
      "verified conclusion [1]",
    );

    transport.historyPages.push(page());
    transport.streamResult.resolve({ status: "completed", runId: "run-1" });
    await running;
  });

  it("unifies approval and stop while rejecting stale or duplicate events", async () => {
    vi.useFakeTimers();
    const transport = new FakeTransport();
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });
    const running = conversations.send(sessionId, { query: "run the tool" });

    transport.emit(event("run.started", 1));
    transport.emit(event("turn.started", 2));
    transport.emit(
      event("tool.approval_required", 3, {
        tool_call_id: "tool-1",
        name: "write_note",
        label: "Write a note",
        arguments: { title: "test" },
        risk: "write",
      }),
    );
    expect(conversations.getSessionSnapshot(sessionId).run?.pendingApproval).toMatchObject({
      toolCallId: "tool-1",
      label: "Write a note",
      resolving: false,
    });

    await conversations.approve(sessionId, "tool-1");
    expect(transport.approvalCalls).toEqual(["approve:tool-1"]);
    expect(conversations.getSessionSnapshot(sessionId).run?.pendingApproval).toBeNull();

    transport.emit(
      event("tool.started", 5, {
        tool_call_id: "tool-1",
        name: "write_note",
        label: "Write a note",
      }),
    );
    transport.emit(
      event("tool.progress", 4, { tool_call_id: "tool-1", message: "stale progress" }),
    );
    transport.emit(
      event(
        "tool.progress",
        6,
        { tool_call_id: "tool-1", message: "wrong run" },
        1,
        "run-other",
      ),
    );
    expect(conversations.getSessionSnapshot(sessionId).run?.steps.at(-1)?.progress).toBeUndefined();

    await conversations.stop(sessionId);
    expect(transport.cancelCalls).toEqual(["run-1"]);
    expect(conversations.getSessionSnapshot(sessionId).run?.lifecycle).toBe("stopping");

    transport.emit(event("run.cancelled", 7, { error: { message: "Run cancelled" } }));
    expect(conversations.getSessionSnapshot(sessionId).error).toBe("Đã dừng");
    transport.streamResult.resolve({ status: "cancelled", runId: "run-1" });
    await running;
    expect(conversations.getSessionSnapshot(sessionId).messages.at(-1)).toMatchObject({
      content: "Đã dừng",
      delivery: "cancelled",
    });
    expect(conversations.getSessionSnapshot(sessionId).messages.at(-1)?.steps.at(-1)).toMatchObject({
      kind: "tool",
      error: "Đã dừng",
    });
  });

  it("coalesces repeated approval actions while the first request is resolving", async () => {
    const transport = new FakeTransport();
    const pendingApproval = deferred<void>();
    transport.approvalResult = pendingApproval.promise;
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });
    const running = conversations.send(sessionId, { query: "run the tool" });

    transport.emit(
      event("tool.approval_required", 1, {
        tool_call_id: "tool-1",
        name: "write_note",
        label: "Write a note",
      }),
    );
    const first = conversations.approve(sessionId, "tool-1");
    const repeated = conversations.approve(sessionId, "tool-1");

    await repeated;
    expect(transport.approvalCalls).toEqual(["approve:tool-1"]);
    expect(conversations.getSessionSnapshot(sessionId).run?.pendingApproval?.resolving).toBe(true);

    pendingApproval.resolve();
    await first;
    expect(conversations.getSessionSnapshot(sessionId).run?.pendingApproval).toBeNull();

    transport.streamResult.resolve({ status: "completed", runId: "run-1" });
    await running;
  });

  it("accepts synthetic universe activation and tool completion at the same sequence", async () => {
    const transport = new FakeTransport();
    const activations: UniverseActivation[] = [];
    const conversations = runtime(transport, {
      onUniverseActivation: (activation) => activations.push(activation),
    });
    const sessionId = conversations.forThread("thread-1", { activate: true });
    const running = conversations.send(sessionId, { query: "look up material" });
    const activation: UniverseActivation = { query: "look up material", nodes: [], relations: [] };

    transport.emit(
      event("tool.started", 1, {
        tool_call_id: "search-1",
        name: "search_context",
        label: "Search the knowledge base",
      }),
    );
    transport.emit(event("universe.activation", 2, activation as unknown as Record<string, unknown>));
    transport.emit(
      event("tool.completed", 2, {
        tool_call_id: "search-1",
        name: "search_context",
        duration_ms: 25,
        details: { count: 2 },
      }),
    );

    expect(activations).toEqual([activation]);
    expect(conversations.getSessionSnapshot(sessionId).run?.steps.at(-1)).toMatchObject({
      status: "done",
      count: 2,
    });

    transport.streamResult.resolve({ status: "completed", runId: "run-1" });
    await running;
  });

  it("aborts owned work only when the runtime is disposed", () => {
    const transport = new FakeTransport();
    const conversations = runtime(transport);
    const sessionId = conversations.forThread("thread-1", { activate: true });
    void conversations.send(sessionId, { query: "a long task" });

    const unsubscribe = conversations.subscribeSession(sessionId, () => {});
    unsubscribe();
    expect(transport.streamSignal?.aborted).toBe(false);

    conversations.dispose();
    expect(transport.streamSignal?.aborted).toBe(true);
  });
});
