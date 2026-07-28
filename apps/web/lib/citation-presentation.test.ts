import { describe, expect, it } from "vitest";

import {
  citationCopy,
  cleanCitationText,
  stripCitationTransportTokens,
} from "./citation-presentation";
import type { Citation } from "./types";

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    n: 1,
    kind: "internal",
    chunk_id: "chunk-1",
    heading: "Release notes",
    snippet: "This is only the raw fragment the search hit; it must not become the event title or summary by itself. More body text follows.",
    score: 0.9,
    source_id: "source-1",
    source_name: "Project material",
    ...overrides,
  };
}

describe("citation presentation", () => {
  it("uses only the first real event reference for an internal title and body", () => {
    expect(
      citationCopy(
        citation({
          event_refs: [
            {
              id: "event-1",
              title: "AI",
              content: "The product is ready and has formally entered public testing.",
              summary: "An event summary that must not be shown.",
            },
            {
              id: "event-2",
              title: "A second event that must not be shown by default",
              content: "The second event body.",
              summary: "The second event summary.",
            },
          ],
        }),
        1,
      ),
    ).toEqual({
      mode: "event",
      title: "AI",
      body: "The product is ready and has formally entered public testing.",
      meta: "",
    });
  });

  it("falls back to a neutral knowledge source and keeps heading/source as metadata", () => {
    const copy = citationCopy(citation(), 1);

    expect(copy).toEqual({
      mode: "source_only",
      title: "Nguồn tri thức 1",
      body: "",
      meta: "Project material · Mục: Release notes",
    });
    expect(copy.title).not.toContain("search hit");
    expect(copy.body).not.toContain("search hit");
  });

  it("never treats a legacy internal summary or heading as event metadata", () => {
    const copy = citationCopy(
      citation({
        heading: "pdf",
        source_name: "pdf",
        summary: "A summary an old client built from the first sentence of the snippet.",
        snippet: "Product introduction, an all-in-one machine. More body text follows.",
      }),
      1,
    );

    expect(copy).toEqual({
      mode: "source_only",
      title: "Nguồn tri thức 1",
      body: "",
      meta: "pdf",
    });
  });

  it("uses only explicit external title, summary, source and URL metadata", () => {
    expect(
      citationCopy(
        citation({
          kind: "external",
          title: "Official release note",
          summary: "The vendor confirmed the new version has shipped.",
          source: "Example Research",
          url: "https://news.example.com/releases/1",
          heading: "a heading that must not be used",
          source_name: "a source_name that must not be used",
          snippet: "A longer body fragment returned by an external tool.",
        }),
        1,
      ),
    ).toEqual({
      mode: "external",
      title: "Official release note",
      body: "The vendor confirmed the new version has shipped.",
      meta: "Example Research · news.example.com",
    });
  });

  it("does not promote an external snippet when explicit title and summary are missing", () => {
    expect(
      citationCopy(
        citation({
          kind: "external",
          title: null,
          summary: undefined,
          source: null,
          url: "https://www.example.com/article",
          snippet: "Body text that must not pose as an external title or summary.",
        }),
        2,
      ),
    ).toEqual({
      mode: "external",
      title: "example.com",
      body: "",
      meta: "",
    });
  });

  it("cleans malformed citation tokens within fields without moving text between fields", () => {
    const copy = citationCopy(
      citation({
        event_refs: [
          {
            title: "## Official update \ue200cite\ue202turn17view2",
            content: "**Already shipped** \ue200cite\ue202turn17view4",
            summary: "a summary that must not be shown",
          },
        ],
        snippet: "`the full fragment` \ue200cite\ue202turn17view5",
      }),
      1,
    );

    expect(copy).toMatchObject({
      title: "Official update",
      body: "Already shipped",
    });
    expect(cleanCitationText("## Executive summary\n**The core conclusion** is in the [report](https://example.com).")).toBe(
      "Executive summary The core conclusion is in the report.",
    );
    expect(
      cleanCitationText(
        "real raw text \ue200cite\ue202turn8view5\ue202turn19view0\ue201 more content",
      ),
    ).toBe("real raw text more content");
    expect(
      stripCitationTransportTokens(
        "paragraph one \ue200cite\ue202turn8view5\ue201\n\nparagraph two **keeps the original formatting**",
      ),
    ).toBe("paragraph one\n\nparagraph two **keeps the original formatting**");
  });

  it("provides a stable source-only fallback when all metadata is empty", () => {
    expect(
      citationCopy(
        citation({ heading: "", source_name: null, snippet: "", n: 3 }),
        1,
      ),
    ).toEqual({
      mode: "source_only",
      title: "Nguồn tri thức 3",
      body: "",
      meta: "",
    });
  });

  it("does not fall back to an event summary or source chunk when content is missing", () => {
    const copy = citationCopy(
      citation({
        snippet: "the retrieved chunk body",
        event_refs: [{ title: "A real event", summary: "a condensed summary" }],
      }),
      1,
    );

    expect(copy).toMatchObject({
      mode: "event",
      title: "A real event",
      body: "",
    });
  });
});
