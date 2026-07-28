import { describe, expect, it } from "vitest";

import {
  SAG_KNOWLEDGE_MCP_SERVER_KEY,
  mcpServerKey,
} from "./mcp-server-key";

describe("MCP server configuration keys", () => {
  it("uses a stable protocol-safe key for the knowledge service", () => {
    expect(SAG_KNOWLEDGE_MCP_SERVER_KEY).toBe("sag-knowledge");
    expect(SAG_KNOWLEDGE_MCP_SERVER_KEY).toMatch(/^[a-zA-Z0-9_-]+$/);
  });

  it("repairs display names and falls back for non-ASCII source names", () => {
    expect(mcpServerKey("My Research Notes")).toBe("My-Research-Notes");
    expect(mcpServerKey("Café")).toBe("Cafe");
    expect(mcpServerKey("\u4e2d\u6587\u8d44\u6599", "sag-source-abc_123")).toBe("sag-source-abc_123");
  });
});
