"use client";

import * as React from "react";
import { Download, KeyRound, Lock, LockOpen, RotateCw, Save, Upload } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { ProviderAttemptLog } from "@/components/features/provider-attempt-log";
import { ProviderChainEditor } from "@/components/features/provider-chain-editor";
import { SecureConfigTransferDialog } from "@/components/features/secure-config-transfer-dialog";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Slider } from "@/components/ui/slider";
import { Spinner } from "@/components/ui/spinner";
import { api, ApiError } from "@/lib/api";
import {
  createSettingsTransfer,
  datedJsonFilename,
  downloadJsonFile,
  isModelProviderId,
  parsePortableConfigBundle,
  parseSettingsTransfer,
} from "@/lib/settings-config-transfer";
import type {
  LLMProviderEntryInput,
  ModelConfig,
  ModelConfigPatch,
  ModelProviderSpec,
  PortableConfigBundle,
  ProviderAttempt,
  ProviderHealth,
} from "@/lib/types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function optionalNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function ModelConfigForm() {
  const t = useTranslations("ModelConfig");
  const { refreshCapabilities } = useApp();
  const [cfg, setCfg] = React.useState<ModelConfig | null>(null);
  const [providers, setProviders] = React.useState<ModelProviderSpec[]>([]);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [secureBusy, setSecureBusy] = React.useState(false);
  const [secureMode, setSecureMode] = React.useState<"export" | "import" | null>(null);
  const [pendingPortableBundle, setPendingPortableBundle] =
    React.useState<PortableConfigBundle | null>(null);
  const importInputRef = React.useRef<HTMLInputElement>(null);

  const [entries, setEntries] = React.useState<LLMProviderEntryInput[]>([]);
  const [health, setHealth] = React.useState<ProviderHealth[]>([]);
  const [attempts, setAttempts] = React.useState<ProviderAttempt[]>([]);
  const [temperature, setTemperature] = React.useState(0.3);
  const [maxTokens, setMaxTokens] = React.useState(20_000);
  const [timeoutMs, setTimeoutMs] = React.useState(360_000);
  const [maxRetries, setMaxRetries] = React.useState(2);
  const [ctxWindow, setCtxWindow] = React.useState(128000);
  const [embModel, setEmbModel] = React.useState("");
  const [embBaseUrl, setEmbBaseUrl] = React.useState("");
  const [embKey, setEmbKey] = React.useState("");
  const [embDims, setEmbDims] = React.useState("");
  // Đổi model embedding = đổi không gian vector, index cũ thành rác. Khoá lại theo mặc định,
  // Bệ hạ phải mở khoá một cách chủ ý mới sửa được — và chỉ khi mở khoá mới gửi field này lên.
  const [embUnlocked, setEmbUnlocked] = React.useState(false);

  const hydrate = React.useCallback((config: ModelConfig) => {
    setCfg(config);
    // Server không trả key: mỗi entry về với api_key rỗng + cờ cho biết đã có key hay chưa.
    setEntries(
      config.llm_providers.map((entry) => ({
        id: entry.id,
        provider: entry.provider,
        model: entry.model,
        label: entry.label,
        base_url: entry.base_url,
        priority: entry.priority,
        enabled: entry.enabled,
        extra_body: entry.extra_body,
        cooldown_seconds: entry.cooldown_seconds,
        temperature: entry.temperature,
        max_tokens: entry.max_tokens,
        timeout_ms: entry.timeout_ms,
        max_retries: entry.max_retries,
        api_key: "",
        api_key_set_hint: entry.api_key_set,
      })),
    );
    setTemperature(config.llm_temperature);
    setMaxTokens(config.llm_max_tokens);
    setTimeoutMs(config.llm_timeout_ms ?? 360_000);
    setMaxRetries(config.llm_max_retries ?? 2);
    setCtxWindow(config.llm_context_window ?? 128000);
    setEmbModel(config.embedding_model);
    setEmbBaseUrl(config.embedding_base_url ?? "");
    setEmbDims(config.embedding_dimensions != null ? String(config.embedding_dimensions) : "");
    setEmbKey("");
    setEmbUnlocked(false);
  }, []);

  const loadRuntime = React.useCallback(async () => {
    try {
      const { attempts: recent, health: snapshot } = await api.providerAttempts(30);
      setAttempts(recent);
      setHealth(snapshot);
    } catch {
      // Lịch sử gọi là thông tin phụ trợ: không có thì thôi, đừng chặn cả trang cấu hình.
    }
  }, []);

  const load = React.useCallback(async () => {
    setLoadError(null);
    try {
      const [config, providerCatalog] = await Promise.all([
        api.getModelConfig(),
        api.getModelProviders(),
      ]);
      setProviders(providerCatalog);
      hydrate(config);
      void loadRuntime();
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : t("loadFailed"));
    }
  }, [hydrate, loadRuntime, t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  function currentPatch(): ModelConfigPatch {
    const patch: ModelConfigPatch = {
      llm_providers: entries.map((entry) => ({
        ...entry,
        model: entry.model.trim(),
        label: entry.label.trim(),
        base_url: entry.base_url?.trim() ? entry.base_url.trim() : null,
        api_key: entry.api_key?.trim() || undefined,
      })),
      llm_temperature: temperature,
      llm_max_tokens: maxTokens,
      llm_timeout_ms: timeoutMs,
      llm_max_retries: maxRetries,
      llm_context_window: ctxWindow,
    };
    // Còn khoá thì không gửi gì thuộc embedding — tránh việc một state cũ trên client
    // ghi đè cấu hình đang chạy mà Bệ hạ không hề chạm vào.
    if (embUnlocked) {
      patch.embedding_model = embModel.trim();
      patch.embedding_base_url = embBaseUrl.trim();
      patch.embedding_dimensions = embDims.trim() ? Number(embDims) : null;
      if (embKey.trim()) patch.embedding_api_key = embKey.trim();
    }
    return patch;
  }

  async function save() {
    setSaving(true);
    try {
      const patch = currentPatch();
      const { config } = await api.saveModelConfig(patch);
      hydrate(config);
      await refreshCapabilities();
      void loadRuntime();
      toast.success(t("saved"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  function exportConfig() {
    const patch = currentPatch();
    const bundle = createSettingsTransfer("alice-model-config", {
      ...patch,
      // Embedding bị khoá chỉ để chống sửa nhầm; export vẫn phải mang cấu hình đang thấy.
      embedding_model: embModel.trim(),
      embedding_base_url: embBaseUrl.trim() || null,
      embedding_dimensions: embDims.trim() ? Number(embDims) : null,
    });
    downloadJsonFile(datedJsonFilename("alice-model-config"), bundle);
    toast.success(t("exported"));
  }

  async function importConfig(file: File) {
    try {
      const text = await file.text();
      const portable = parsePortableConfigBundle(text, "alice-model-config");
      if (portable) {
        setPendingPortableBundle(portable);
        setSecureMode("import");
        return;
      }
      const bundle = parseSettingsTransfer(text, "alice-model-config");
      const imported = bundle.config;
      if (!Array.isArray(imported.llm_providers)) throw new Error("invalid_providers");

      const currentById = new Map(entries.map((entry) => [entry.id, entry]));
      const nextEntries: LLMProviderEntryInput[] = imported.llm_providers.map((raw, index) => {
        if (!isRecord(raw) || typeof raw.id !== "string" || !isModelProviderId(raw.provider)) {
          throw new Error("invalid_provider");
        }
        if (!providers.some((provider) => provider.id === raw.provider)) {
          throw new Error("unknown_provider");
        }
        const previous = currentById.get(raw.id);
        const baseUrl = optionalString(raw.base_url);
        const canKeepKey = Boolean(
          previous?.api_key_set_hint &&
            previous.provider === raw.provider &&
            (previous.base_url ?? null) === baseUrl,
        );
        return {
          id: raw.id,
          provider: raw.provider,
          model: typeof raw.model === "string" ? raw.model : "",
          label: typeof raw.label === "string" ? raw.label : "",
          base_url: baseUrl,
          priority: optionalNumber(raw.priority) ?? index,
          enabled: raw.enabled !== false,
          extra_body: isRecord(raw.extra_body) ? raw.extra_body : null,
          cooldown_seconds: optionalNumber(raw.cooldown_seconds) ?? 60,
          temperature: optionalNumber(raw.temperature),
          max_tokens: optionalNumber(raw.max_tokens),
          timeout_ms: optionalNumber(raw.timeout_ms),
          max_retries: optionalNumber(raw.max_retries),
          api_key: "",
          api_key_set_hint: canKeepKey,
        };
      });
      setEntries(nextEntries);

      if (optionalNumber(imported.llm_temperature) !== null) {
        setTemperature(optionalNumber(imported.llm_temperature) as number);
      }
      if (optionalNumber(imported.llm_max_tokens) !== null) {
        setMaxTokens(optionalNumber(imported.llm_max_tokens) as number);
      }
      if (optionalNumber(imported.llm_timeout_ms) !== null) {
        setTimeoutMs(optionalNumber(imported.llm_timeout_ms) as number);
      }
      if (optionalNumber(imported.llm_max_retries) !== null) {
        setMaxRetries(optionalNumber(imported.llm_max_retries) as number);
      }
      if (optionalNumber(imported.llm_context_window) !== null) {
        setCtxWindow(optionalNumber(imported.llm_context_window) as number);
      }

      const nextEmbeddingModel = Object.hasOwn(imported, "embedding_model")
        ? optionalString(imported.embedding_model) ?? embModel
        : embModel;
      const nextEmbeddingBaseUrl = Object.hasOwn(imported, "embedding_base_url")
        ? optionalString(imported.embedding_base_url) ?? ""
        : embBaseUrl;
      const nextEmbeddingDimensions = Object.hasOwn(imported, "embedding_dimensions")
        ? optionalNumber(imported.embedding_dimensions)
        : embDims.trim()
          ? optionalNumber(Number(embDims))
          : null;
      const embeddingChanged =
        nextEmbeddingModel !== embModel ||
        nextEmbeddingBaseUrl !== embBaseUrl ||
        (nextEmbeddingDimensions === null ? "" : String(nextEmbeddingDimensions)) !== embDims;
      setEmbModel(nextEmbeddingModel);
      setEmbBaseUrl(nextEmbeddingBaseUrl);
      setEmbDims(nextEmbeddingDimensions === null ? "" : String(nextEmbeddingDimensions));
      setEmbKey("");
      if (embeddingChanged) setEmbUnlocked(true);

      toast.success(t("imported", { count: nextEntries.length }));
    } catch {
      toast.error(t("importFailed"));
    } finally {
      if (importInputRef.current) importInputRef.current.value = "";
    }
  }

  async function transferEncrypted(passphrase: string) {
    setSecureBusy(true);
    try {
      if (secureMode === "export") {
        const bundle = await api.exportPortableConfig("alice-model-config", passphrase);
        downloadJsonFile(datedJsonFilename("alice-model-config-encrypted"), bundle);
        toast.success(t("secureExported"));
      } else {
        if (!pendingPortableBundle) throw new Error("missing_bundle");
        await api.importPortableConfig(pendingPortableBundle, passphrase);
        await load();
        await refreshCapabilities();
        toast.success(t("secureImported"));
      }
      setSecureMode(null);
      setPendingPortableBundle(null);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("secureTransferFailed"));
    } finally {
      setSecureBusy(false);
    }
  }

  if (loadError) {
    return (
      <SettingsSection title={t("title")} description={t("description")}>
        <div className="p-4 sm:p-5">
          <Alert variant="destructive">
            <AlertTitle>{t("loadErrorTitle")}</AlertTitle>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>{loadError}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => void load()}>
                <RotateCw />
                {t("retry")}
              </Button>
            </AlertDescription>
          </Alert>
        </div>
      </SettingsSection>
    );
  }

  if (!cfg || providers.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {[
          [t("generationTitle"), t("generationLoading")],
          [t("embeddingTitle"), t("embeddingLoading")],
        ].map(([title, description]) => (
          <SettingsSection key={title} title={title} description={description}>
            <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          </SettingsSection>
        ))}
      </div>
    );
  }

  // Tham số hành vi (temperature / embedding tái dùng credential) áp theo provider đầu chuỗi —
  // đó là nhà mặc định, và cũng là nhà mà embedding có thể mượn credential.
  const headEntry = [...entries]
    .filter((entry) => entry.enabled)
    .sort((a, b) => a.priority - b.priority)[0];
  const providerSpec =
    providers.find((provider) => provider.id === headEntry?.provider) ?? providers[0];

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("generationTitle")} description={t("generationDescription")}>
        <SettingsRow title={t("chainTitle")} description={t("chainDescription")}>
          <ProviderChainEditor
            entries={entries}
            providers={providers}
            health={health}
            onChange={setEntries}
          />
        </SettingsRow>

        <SettingsRow title={t("attemptsTitle")} description={t("attemptsDescription")}>
          <ProviderAttemptLog attempts={attempts} onRefresh={() => void loadRuntime()} />
        </SettingsRow>

        <SettingsRow title={t("generationParams")} description={t("generationParamsDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="llm-ctxwin">{t("contextWindow")}</FieldLabel>
              <Input
                id="llm-ctxwin"
                type="number"
                min={1024}
                max={2000000}
                value={ctxWindow}
                onChange={(event) =>
                  setCtxWindow(Math.max(1024, Number(event.target.value) || 1024))
                }
              />
              <FieldDescription>{t("contextWindowDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="llm-maxtok">{t("maxOutputTokens")}</FieldLabel>
              <Input
                id="llm-maxtok"
                type="number"
                min={1}
                max={32768}
                value={maxTokens}
                onChange={(event) =>
                  setMaxTokens(Math.max(1, Number(event.target.value) || 1))
                }
              />
            </Field>
            <Field>
              <FieldLabel>
                {t("temperature", {
                  value: (
                    providerSpec.temperature_configurable
                      ? temperature
                      : providerSpec.default_temperature
                  ).toFixed(1),
                })}
              </FieldLabel>
              <div className="flex h-9 items-center">
                <Slider
                  value={[
                    providerSpec.temperature_configurable
                      ? temperature
                      : providerSpec.default_temperature,
                  ]}
                  min={0}
                  max={2}
                  step={0.1}
                  disabled={!providerSpec.temperature_configurable}
                  onValueChange={([value]) => setTemperature(value)}
                />
              </div>
              <FieldDescription>
                {t(
                  !providerSpec.temperature_configurable
                    ? "fixedTemperatureDescription"
                    : "temperatureDescription",
                )}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="llm-timeout">{t("timeout")}</FieldLabel>
              <Input
                id="llm-timeout"
                type="number"
                min={1000}
                max={600000}
                step={1000}
                value={timeoutMs}
                onChange={(event) =>
                  setTimeoutMs(
                    Math.min(600000, Math.max(1000, Number(event.target.value) || 1000)),
                  )
                }
              />
              <FieldDescription>{t("timeoutDescription")}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="llm-retries">{t("retries")}</FieldLabel>
              <Input
                id="llm-retries"
                type="number"
                min={0}
                max={10}
                step={1}
                value={maxRetries}
                onChange={(event) =>
                  setMaxRetries(Math.min(10, Math.max(0, Number(event.target.value) || 0)))
                }
              />
              <FieldDescription>{t("retriesDescription")}</FieldDescription>
            </Field>
          </div>
        </SettingsRow>
      </SettingsSection>

      <SettingsSection title={t("embeddingTitle")} description={t("embeddingDescription")}>
        <SettingsRow
          title={t("modelAndConnection")}
          description={t(
            providerSpec.can_reuse_embedding_credentials
              ? "embeddingConnectionDescription"
              : "embeddingNativeConnectionDescription",
          )}
        >
          <div className="flex flex-col gap-4">
            <Alert>
              <AlertTitle>{t("embeddingLockTitle")}</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span className="min-w-0">{t("embeddingLockWarning")}</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setEmbUnlocked((unlocked) => !unlocked)}
                >
                  {embUnlocked ? <LockOpen /> : <Lock />}
                  {embUnlocked ? t("embeddingRelock") : t("embeddingUnlock")}
                </Button>
              </AlertDescription>
            </Alert>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="emb-model">{t("model")}</FieldLabel>
                <Input
                  id="emb-model"
                  value={embModel}
                  disabled={!embUnlocked}
                  onChange={(event) => setEmbModel(event.target.value)}
                  placeholder="bge-large-en-v1.5"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="emb-dims">{t("dimensions")}</FieldLabel>
                <Input
                  id="emb-dims"
                  type="number"
                  min={1}
                  max={8192}
                  value={embDims}
                  disabled={!embUnlocked}
                  onChange={(event) => setEmbDims(event.target.value)}
                  placeholder={t("modelDefault")}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="emb-url">{t("optionalBaseUrl")}</FieldLabel>
                <Input
                  id="emb-url"
                  value={embBaseUrl}
                  disabled={!embUnlocked}
                  onChange={(event) => setEmbBaseUrl(event.target.value)}
                  placeholder="http://embedding:11434/v1"
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="emb-key">{t("optionalApiKey")}</FieldLabel>
                <Input
                  id="emb-key"
                  type="password"
                  autoComplete="off"
                  value={embKey}
                  disabled={!embUnlocked}
                  onChange={(event) => setEmbKey(event.target.value)}
                  placeholder={
                    cfg.embedding_api_key_set
                      ? t("keyConfigured")
                      : providerSpec.can_reuse_embedding_credentials
                        ? t("reuseGeneration")
                        : t("separateEmbeddingKey")
                  }
                />
              </Field>
            </div>
            {embUnlocked && (
              <p className="text-destructive text-sm leading-5">{t("embeddingUnlockedHint")}</p>
            )}
          </div>
        </SettingsRow>
      </SettingsSection>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
        <div className="min-w-0">
          <p className="text-muted-foreground text-sm">{t("saveHint")}</p>
          <p className="text-muted-foreground mt-1 text-xs">{t("transferHint")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={importInputRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importConfig(file);
            }}
          />
          <Button type="button" variant="outline" onClick={exportConfig}>
            <Download />
            {t("export")}
          </Button>
          <Button type="button" variant="outline" onClick={() => setSecureMode("export")}>
            <KeyRound />
            {t("exportEncrypted")}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => importInputRef.current?.click()}
          >
            <Upload />
            {t("import")}
          </Button>
          <Button type="button" onClick={save} disabled={saving}>
            {saving ? <Spinner /> : <Save />}
            {saving ? t("saving") : t("save")}
          </Button>
        </div>
      </div>
      <SecureConfigTransferDialog
        open={secureMode !== null}
        mode={secureMode ?? "export"}
        description={t(
          secureMode === "import" ? "secureImportDescription" : "secureExportDescription",
        )}
        busy={secureBusy}
        onOpenChange={(open) => {
          if (!open) {
            setSecureMode(null);
            setPendingPortableBundle(null);
          }
        }}
        onSubmit={transferEncrypted}
      />
    </div>
  );
}
