"use client";

import * as React from "react";
import { Check, Plug, RotateCw, Save, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { useApp } from "@/components/features/app-shell";
import { SettingsRow, SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Slider } from "@/components/ui/slider";
import { Spinner } from "@/components/ui/spinner";
import { api, ApiError } from "@/lib/api";
import type {
  ModelConfig,
  ModelConfigPatch,
  ModelProviderId,
  ModelProviderSpec,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function ModelConfigForm() {
  const t = useTranslations("ModelConfig");
  const { refreshCapabilities } = useApp();
  const [cfg, setCfg] = React.useState<ModelConfig | null>(null);
  const [providers, setProviders] = React.useState<ModelProviderSpec[]>([]);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [testResult, setTestResult] = React.useState<{ ok: boolean; message: string } | null>(null);

  const [llmProvider, setLlmProvider] = React.useState<ModelProviderId>("openai");
  const [llmBaseUrl, setLlmBaseUrl] = React.useState("");
  const [llmKey, setLlmKey] = React.useState("");
  const [llmModel, setLlmModel] = React.useState("");
  const [temperature, setTemperature] = React.useState(0.3);
  const [maxTokens, setMaxTokens] = React.useState(20_000);
  const [timeoutMs, setTimeoutMs] = React.useState(60_000);
  const [maxRetries, setMaxRetries] = React.useState(2);
  const [ctxWindow, setCtxWindow] = React.useState(128000);
  const [embModel, setEmbModel] = React.useState("");
  const [embBaseUrl, setEmbBaseUrl] = React.useState("");
  const [embKey, setEmbKey] = React.useState("");
  const [embDims, setEmbDims] = React.useState("");
  const [documentParser, setDocumentParser] =
    React.useState<ModelConfig["document_parser"]>("markitdown");

  const hydrate = React.useCallback((config: ModelConfig) => {
    setCfg(config);
    setLlmProvider(config.llm_provider);
    setLlmBaseUrl(config.llm_base_url ?? "");
    setLlmModel(config.llm_model);
    setTemperature(config.llm_temperature);
    setMaxTokens(config.llm_max_tokens);
    setTimeoutMs(config.llm_timeout_ms ?? 60_000);
    setMaxRetries(config.llm_max_retries ?? 2);
    setCtxWindow(config.llm_context_window ?? 128000);
    setEmbModel(config.embedding_model);
    setEmbBaseUrl(config.embedding_base_url ?? "");
    setEmbDims(config.embedding_dimensions != null ? String(config.embedding_dimensions) : "");
    setDocumentParser(config.document_parser);
    setLlmKey("");
    setEmbKey("");
  }, []);

  const load = React.useCallback(async () => {
    setLoadError(null);
    try {
      const [config, providerCatalog] = await Promise.all([
        api.getModelConfig(),
        api.getModelProviders(),
      ]);
      if (!providerCatalog.some((provider) => provider.id === config.llm_provider)) {
        throw new Error("Configured model provider is missing from the provider catalog");
      }
      setProviders(providerCatalog);
      hydrate(config);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : t("loadFailed"));
    }
  }, [hydrate, t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  function currentPatch(): ModelConfigPatch {
    const patch: ModelConfigPatch = {
      llm_provider: llmProvider,
      llm_base_url: llmBaseUrl.trim() || null,
      llm_model: llmModel.trim(),
      llm_temperature: temperature,
      llm_max_tokens: maxTokens,
      llm_timeout_ms: timeoutMs,
      llm_max_retries: maxRetries,
      llm_context_window: ctxWindow,
      embedding_model: embModel.trim(),
      embedding_base_url: embBaseUrl.trim(),
      embedding_dimensions: embDims.trim() ? Number(embDims) : null,
      document_parser: documentParser,
    };
    if (llmKey.trim()) patch.llm_api_key = llmKey.trim();
    if (embKey.trim()) patch.embedding_api_key = embKey.trim();
    return patch;
  }

  async function save() {
    setSaving(true);
    setTestResult(null);
    try {
      const patch = currentPatch();
      const { config } = await api.saveModelConfig(patch);
      hydrate(config);
      await refreshCapabilities();
      toast.success(t("saved"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await api.testModelConfig(currentPatch()));
    } catch (error) {
      setTestResult({
        ok: false,
        message: error instanceof ApiError ? error.message : t("testFailed"),
      });
    } finally {
      setTesting(false);
    }
  }

  function changeProvider(value: string) {
    const next = providers.find((provider) => provider.id === value);
    const current = providers.find((provider) => provider.id === llmProvider);
    if (!next) return;
    const knownUrls = new Set(
      providers.map((provider) => provider.default_base_url).filter(Boolean),
    );
    const knownModels = new Set(providers.map((provider) => provider.default_model));
    const knownContextWindows = new Set(
      providers.map((provider) => provider.default_context_window),
    );
    if (!llmBaseUrl.trim() || knownUrls.has(llmBaseUrl.trim())) {
      setLlmBaseUrl(next.default_base_url ?? "");
    }
    if (!llmModel.trim() || knownModels.has(llmModel.trim())) {
      setLlmModel(next.default_model);
    }
    if (knownContextWindows.has(ctxWindow)) {
      setCtxWindow(next.default_context_window);
    }
    if (
      !next.temperature_configurable ||
      !current ||
      !current.temperature_configurable ||
      temperature === current.default_temperature
    ) {
      setTemperature(next.default_temperature);
    }
    setLlmProvider(next.id);
    setTestResult(null);
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
          [t("parserTitle"), t("parserLoading")],
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

  const providerSpec = providers.find((provider) => provider.id === llmProvider)!;

  const generationKeyPlaceholder =
    cfg.llm_api_key_set && cfg.llm_provider === llmProvider
      ? t("keyConfigured")
      : providerSpec.api_key_placeholder;

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("generationTitle")} description={t("generationDescription")}>
        <SettingsRow title={t("connectionTitle")} description={t("connectionDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="llm-provider">{t("provider")}</FieldLabel>
              <Select value={llmProvider} onValueChange={changeProvider}>
                <SelectTrigger id="llm-provider">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {providers.map((provider) => (
                    <SelectItem key={provider.id} value={provider.id}>
                      {provider.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <FieldDescription>{t(`providerDescription.${llmProvider}`)}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="llm-url">Base URL</FieldLabel>
              <Input
                id="llm-url"
                value={llmBaseUrl}
                onChange={(event) => setLlmBaseUrl(event.target.value)}
                placeholder={providerSpec.default_base_url ?? t("officialEndpoint")}
              />
              <FieldDescription>{t(`baseUrlDescription.${llmProvider}`)}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="llm-key">API Key</FieldLabel>
              <Input
                id="llm-key"
                type="password"
                autoComplete="off"
                value={llmKey}
                onChange={(event) => setLlmKey(event.target.value)}
                placeholder={generationKeyPlaceholder}
              />
              <FieldDescription>
                {cfg.llm_provider !== llmProvider && cfg.llm_api_key_set
                  ? t("providerChangedKeyDescription")
                  : t("secretDescription")}
              </FieldDescription>
            </Field>
          </div>
        </SettingsRow>

        <SettingsRow title={t("generationParams")} description={t("generationParamsDescription")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="llm-model">{t("model")}</FieldLabel>
              <Input
                id="llm-model"
                value={llmModel}
                onChange={(event) => setLlmModel(event.target.value)}
                placeholder={providerSpec.default_model}
              />
            </Field>
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
          <div className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="emb-model">{t("model")}</FieldLabel>
              <Input
                id="emb-model"
                value={embModel}
                onChange={(event) => setEmbModel(event.target.value)}
                placeholder="bge-large-zh-v1.5"
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
                onChange={(event) => setEmbDims(event.target.value)}
                placeholder={t("modelDefault")}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="emb-url">{t("optionalBaseUrl")}</FieldLabel>
              <Input
                id="emb-url"
                value={embBaseUrl}
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
        </SettingsRow>
      </SettingsSection>

      <SettingsSection
        title={t("parserTitle")}
        description={t("parserDescription")}
      >
        <SettingsRow title={t("parserEngine")}>
          {/* Chỉ còn MarkItDown chạy cục bộ — không có lựa chọn nào khác nên
              hiển thị dạng thông tin thay vì dropdown một mục. */}
          <Field>
            <FieldLabel htmlFor="document-parser">{t("parserMethod")}</FieldLabel>
            <Input id="document-parser" value="MarkItDown" readOnly disabled />
            <FieldDescription>{t("markitdownDescription")}</FieldDescription>
          </Field>
        </SettingsRow>
      </SettingsSection>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
        <div className="min-h-5 min-w-0">
          {testResult && (
            <span
              className={cn(
                "inline-flex items-center gap-1.5 text-sm",
                testResult.ok ? "text-success" : "text-destructive",
              )}
            >
              {testResult.ok ? <Check className="size-4" /> : <X className="size-4" />}
              {testResult.message}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" onClick={test} variant="outline" disabled={testing || saving}>
            {testing ? <Spinner /> : <Plug />}
            {testing ? t("testing") : t("testGeneration")}
          </Button>
          <Button type="button" onClick={save} disabled={saving || testing}>
            {saving ? <Spinner /> : <Save />}
            {saving ? t("saving") : t("save")}
          </Button>
        </div>
      </div>
    </div>
  );
}
