import { describe, expect, it } from "vitest";

import {
  createSettingsTransfer,
  parseSettingsTransfer,
  parsePortableConfigBundle,
  withoutSecrets,
} from "./settings-config-transfer";

describe("settings config transfer", () => {
  it("removes credentials recursively from exported provider configuration", () => {
    const bundle = createSettingsTransfer(
      "alice-model-config",
      {
        llm_providers: [
          {
            id: "primary",
            provider: "openai",
            model: "gpt-test",
            api_key: "must-not-leak",
            api_key_set_hint: true,
            extra_body: {
              route: "fast",
              authorization: "Bearer must-not-leak",
              max_tokens: 32,
            },
          },
        ],
        embedding_api_key: "must-not-leak",
      },
      "2026-08-01T00:00:00.000Z",
    );

    const text = JSON.stringify(bundle);
    expect(text).not.toContain("must-not-leak");
    expect(text).not.toContain("api_key");
    expect(text).not.toContain("authorization");
    expect(text).toContain("max_tokens");
    expect(text).toContain("route");
    expect(bundle.contains_secrets).toBe(false);
  });

  it("scrubs a hand-edited import instead of trusting its metadata", () => {
    const parsed = parseSettingsTransfer(
      JSON.stringify({
        kind: "alice-sub-agent-config",
        version: 1,
        contains_secrets: true,
        config: { entries: [{ provider: "custom", credential: "secret", model: "x" }] },
      }),
      "alice-sub-agent-config",
    );

    expect(parsed.config).toEqual({ entries: [{ provider: "custom", model: "x" }] });
    expect(parsed.contains_secrets).toBe(false);
  });

  it("rejects the wrong config family and unsupported versions", () => {
    expect(() =>
      parseSettingsTransfer(
        JSON.stringify({ kind: "alice-sub-agent-config", version: 1, config: {} }),
        "alice-model-config",
      ),
    ).toThrow("wrong_kind");
    expect(() =>
      parseSettingsTransfer(
        JSON.stringify({ kind: "alice-model-config", version: 2, config: {} }),
        "alice-model-config",
      ),
    ).toThrow("unsupported_version");
  });

  it("preserves ordinary token-count settings", () => {
    expect(withoutSecrets({ max_tokens: 20_000, access_token: "secret" })).toEqual({
      max_tokens: 20_000,
    });
  });
});

describe("parsePortableConfigBundle", () => {
  it("recognizes an encrypted bundle without decrypting it in the browser", () => {
    const bundle = {
      format: "alice-portable-config",
      version: 1,
      kind: "alice-model-config",
      contains_secrets: true,
      cipher: "AES-256-GCM",
      kdf: { name: "scrypt", salt: "salt", n: 16384, r: 8, p: 1 },
      nonce: "nonce",
      ciphertext: "ciphertext",
    };
    expect(parsePortableConfigBundle(JSON.stringify(bundle), "alice-model-config")).toEqual(bundle);
    expect(() =>
      parsePortableConfigBundle(JSON.stringify(bundle), "alice-sub-agent-config"),
    ).toThrow("invalid_portable_bundle");
  });
});
