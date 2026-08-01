import type {
  ModelProviderId,
  PortableConfigBundle,
  SubAgentProviderId,
} from "@/lib/types";

export const SETTINGS_TRANSFER_VERSION = 1;

export type SettingsTransferKind = "alice-model-config" | "alice-sub-agent-config";

export interface SettingsTransferEnvelope {
  kind: SettingsTransferKind;
  version: typeof SETTINGS_TRANSFER_VERSION;
  exported_at: string;
  contains_secrets: false;
  config: Record<string, unknown>;
}

const MODEL_PROVIDERS = new Set<ModelProviderId>(["openai", "anthropic", "gemini"]);
const SUB_AGENT_PROVIDERS = new Set<SubAgentProviderId>([
  "claude",
  "codex",
  "opencode-go",
  "opencode-zen",
  "gemini-cli",
  "custom",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSecretKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/-/g, "_");
  return (
    /(^|_)(api_?key|token|secret|credential|password|authorization)(_|$)/.test(normalized) ||
    normalized === "api_key_set" ||
    normalized === "credential_set"
  );
}

/**
 * Scrub recursively because provider `extra_body` is user-controlled and can itself contain
 * an auth header/token. Export is configuration portability, never a secret backup channel.
 */
export function withoutSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutSecrets);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !isSecretKey(key) && !key.endsWith("_set_hint"))
      .map(([key, nested]) => [key, withoutSecrets(nested)]),
  );
}

export function createSettingsTransfer(
  kind: SettingsTransferKind,
  config: Record<string, unknown>,
  exportedAt = new Date().toISOString(),
): SettingsTransferEnvelope {
  return {
    kind,
    version: SETTINGS_TRANSFER_VERSION,
    exported_at: exportedAt,
    contains_secrets: false,
    config: withoutSecrets(config) as Record<string, unknown>,
  };
}

export function parseSettingsTransfer(
  text: string,
  expectedKind: SettingsTransferKind,
): SettingsTransferEnvelope {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("invalid_json");
  }
  if (!isRecord(parsed) || parsed.kind !== expectedKind) throw new Error("wrong_kind");
  if (parsed.version !== SETTINGS_TRANSFER_VERSION) throw new Error("unsupported_version");
  if (!isRecord(parsed.config)) throw new Error("invalid_config");
  return {
    kind: expectedKind,
    version: SETTINGS_TRANSFER_VERSION,
    exported_at: typeof parsed.exported_at === "string" ? parsed.exported_at : "",
    contains_secrets: false,
    // Scrub imported files too: never let a hand-edited bundle become a credential transport.
    config: withoutSecrets(parsed.config) as Record<string, unknown>,
  };
}

export function isModelProviderId(value: unknown): value is ModelProviderId {
  return typeof value === "string" && MODEL_PROVIDERS.has(value as ModelProviderId);
}

export function isSubAgentProviderId(value: unknown): value is SubAgentProviderId {
  return typeof value === "string" && SUB_AGENT_PROVIDERS.has(value as SubAgentProviderId);
}

export function downloadJsonFile(filename: string, value: unknown): void {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export async function readSettingsTransferFile(
  file: File,
  expectedKind: SettingsTransferKind,
): Promise<SettingsTransferEnvelope> {
  return parseSettingsTransfer(await file.text(), expectedKind);
}

export function parsePortableConfigBundle(
  text: string,
  expectedKind: SettingsTransferKind,
): PortableConfigBundle | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("invalid_json");
  }
  if (!isRecord(parsed) || parsed.format !== "alice-portable-config") return null;
  if (
    parsed.version !== 1 ||
    parsed.kind !== expectedKind ||
    parsed.contains_secrets !== true ||
    parsed.cipher !== "AES-256-GCM" ||
    !isRecord(parsed.kdf) ||
    parsed.kdf.name !== "scrypt" ||
    typeof parsed.kdf.salt !== "string" ||
    typeof parsed.kdf.n !== "number" ||
    typeof parsed.kdf.r !== "number" ||
    typeof parsed.kdf.p !== "number" ||
    typeof parsed.nonce !== "string" ||
    typeof parsed.ciphertext !== "string"
  ) {
    throw new Error("invalid_portable_bundle");
  }
  return parsed as unknown as PortableConfigBundle;
}

export function datedJsonFilename(prefix: string, now = new Date()): string {
  const stamp = now.toISOString().replace(/[:.]/g, "-");
  return `${prefix}-${stamp}.json`;
}
