"use client";

import * as React from "react";
import { Eye, EyeOff, RotateCw, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { SettingsSection } from "@/components/features/settings-section";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { api, ApiError } from "@/lib/api";
import type {
  SubAgentConfig,
  SubAgentEntryInput,
  SubAgentProviderId,
  SubAgentProviderSpec,
} from "@/lib/types";
import { cn } from "@/lib/utils";

function emptyEntry(spec: SubAgentProviderSpec): SubAgentEntryInput {
  return {
    provider: spec.id,
    model: "",
    provider_name: spec.id === "custom" ? "" : spec.display_name,
    base_url: null,
    enabled: false,
    credential: "",
    credential_set_hint: false,
    model_verified: false,
  };
}

function hydrate(config: SubAgentConfig): SubAgentEntryInput[] {
  const saved = new Map(config.entries.map((entry) => [entry.provider, entry]));
  return config.providers.map((spec) => {
    const entry = saved.get(spec.id);
    if (!entry) return emptyEntry(spec);
    return {
      provider: entry.provider,
      model: entry.model,
      provider_name: entry.provider_name,
      base_url: entry.base_url,
      enabled: entry.enabled,
      credential: "",
      credential_set_hint: entry.credential_set,
      model_verified: entry.model_verified,
    };
  });
}

type DiscoverableProviderId = Exclude<SubAgentProviderId, "custom">;

export function SubAgentConfigForm() {
  const t = useTranslations("SubAgentConfig");
  const [providers, setProviders] = React.useState<SubAgentProviderSpec[]>([]);
  const [entries, setEntries] = React.useState<SubAgentEntryInput[]>([]);
  const [visible, setVisible] = React.useState<Set<SubAgentProviderId>>(new Set());
  const [models, setModels] = React.useState<
    Partial<Record<DiscoverableProviderId, string[]>>
  >({});
  const [discovering, setDiscovering] = React.useState<Set<DiscoverableProviderId>>(
    new Set(),
  );
  const [discoveryErrors, setDiscoveryErrors] = React.useState<
    Partial<Record<DiscoverableProviderId, string>>
  >({});
  const [loading, setLoading] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const config = await api.getSubAgentConfig();
      setProviders(config.providers);
      setEntries(hydrate(config));
      setModels({});
      setDiscoveryErrors({});
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  React.useEffect(() => {
    void load();
  }, [load]);

  function update(provider: SubAgentProviderId, patch: Partial<SubAgentEntryInput>) {
    setEntries((current) =>
      current.map((entry) => (entry.provider === provider ? { ...entry, ...patch } : entry)),
    );
  }

  function toggleVisible(provider: SubAgentProviderId) {
    setVisible((current) => {
      const next = new Set(current);
      if (next.has(provider)) next.delete(provider);
      else next.add(provider);
      return next;
    });
  }

  async function discoverModels(provider: DiscoverableProviderId) {
    const entry = entries.find((item) => item.provider === provider);
    if (!entry) return;
    setDiscovering((current) => new Set(current).add(provider));
    setDiscoveryErrors((current) => ({ ...current, [provider]: undefined }));
    try {
      const result = await api.getSubAgentModels(
        provider,
        entry.credential?.trim() || undefined,
      );
      setModels((current) => ({ ...current, [provider]: result.models }));
      setEntries((current) =>
        current.map((item) =>
          item.provider === provider
            ? {
                ...item,
                model: result.models.includes(item.model) ? item.model : "",
                model_verified: result.models.includes(item.model),
              }
            : item,
        ),
      );
      toast.success(t("modelsLoaded", { count: result.models.length }));
    } catch (error) {
      let message = t("modelsLoadFailed");
      if (error instanceof ApiError) {
        if (error.code === "sub_agent_credential_required") {
          message = t("discoveryError.credentialRequired");
        } else if (error.code === "sub_agent_credential_invalid") {
          message = t("discoveryError.credentialInvalid");
        } else if (error.code === "sub_agent_provider_rate_limited") {
          message = t("discoveryError.rateLimited");
        } else if (
          error.code === "sub_agent_provider_timeout" ||
          error.code === "sub_agent_provider_unavailable"
        ) {
          message = t("discoveryError.providerUnavailable");
        } else if (error.code === "sub_agent_models_empty") {
          message = t("discoveryError.modelsEmpty");
        } else if (error.code === "sub_agent_credential_check_failed") {
          // Gateway trả lỗi mơ hồ; giữ nguyên lời của nó thay vì đoán hộ là "key sai".
          message = error.message || t("discoveryError.checkFailed");
        } else {
          message = t("discoveryError.generic");
        }
      }
      setModels((current) => ({ ...current, [provider]: [] }));
      setEntries((current) =>
        current.map((item) =>
          item.provider === provider
            ? { ...item, model: "", model_verified: false }
            : item,
        ),
      );
      setDiscoveryErrors((current) => ({ ...current, [provider]: message }));
    } finally {
      setDiscovering((current) => {
        const next = new Set(current);
        next.delete(provider);
        return next;
      });
    }
  }

  async function save() {
    setSaving(true);
    try {
      const payload = entries.map((entry) => ({
        provider: entry.provider,
        model: entry.model.trim(),
        provider_name: entry.provider_name.trim(),
        base_url: entry.base_url?.trim() || null,
        enabled: entry.enabled,
        credential: entry.credential?.trim() || undefined,
      }));
      const config = await api.saveSubAgentConfig(payload);
      setEntries(hydrate(config));
      setVisible(new Set());
      toast.success(t("saved"));
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : t("saveFailed"));
    } finally {
      setSaving(false);
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

  if (loading) {
    return (
      <SettingsSection title={t("title")} description={t("description")}>
        <div className="grid gap-3 p-4 sm:grid-cols-2 sm:p-5">
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-44 w-full" />
          ))}
        </div>
      </SettingsSection>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <SettingsSection title={t("title")} description={t("description")}>
        <div className="grid gap-4 p-4 sm:grid-cols-2 sm:p-5">
          {providers.map((spec) => {
            const entry = entries.find((item) => item.provider === spec.id) ?? emptyEntry(spec);
            const credentialVisible = visible.has(spec.id);
            const discoverable =
              spec.model_discovery && spec.id !== "custom"
                ? (spec.id as DiscoverableProviderId)
                : null;
            const liveModels = discoverable ? (models[discoverable] ?? []) : [];
            const modelsLoading = discoverable ? discovering.has(discoverable) : false;
            const discoveryError = discoverable
              ? discoveryErrors[discoverable]
              : undefined;
            return (
              <div
                key={spec.id}
                className={cn(
                  "flex flex-col gap-4 rounded-lg border p-4",
                  !entry.enabled && "bg-muted/20",
                )}
              >
                <div className="flex items-start gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-semibold">{spec.display_name}</h3>
                      {entry.credential_set_hint && (
                        <Badge variant="secondary">{t("credentialStored")}</Badge>
                      )}
                      {discoverable && liveModels.length > 0 && (
                        <Badge variant="outline">{t("modelsLive")}</Badge>
                      )}
                    </div>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      {t(`providerDescription.${spec.id}`)}
                    </p>
                  </div>
                  <Switch
                    className="ms-auto shrink-0"
                    checked={entry.enabled}
                    aria-label={t("enabledAria", { provider: spec.display_name })}
                    onCheckedChange={(enabled) => update(spec.id, { enabled })}
                  />
                </div>

                {spec.custom_model && (
                  <Field>
                    <FieldLabel htmlFor={`${spec.id}-name`}>{t("providerName")}</FieldLabel>
                    <Input
                      id={`${spec.id}-name`}
                      value={entry.provider_name}
                      onChange={(event) =>
                        update(spec.id, { provider_name: event.target.value })
                      }
                      placeholder={t("providerNamePlaceholder")}
                    />
                  </Field>
                )}

                <Field>
                  <FieldLabel htmlFor={`${spec.id}-model`}>{t("model")}</FieldLabel>
                  {spec.custom_model ? (
                    <Input
                      id={`${spec.id}-model`}
                      value={entry.model}
                      onChange={(event) => update(spec.id, { model: event.target.value })}
                      placeholder={t("modelPlaceholder")}
                    />
                  ) : (
                    <Select
                      value={liveModels.includes(entry.model) ? entry.model : ""}
                      disabled={!liveModels.length}
                      onValueChange={(model) =>
                        update(spec.id, { model, model_verified: true })
                      }
                    >
                      <SelectTrigger id={`${spec.id}-model`}>
                        <SelectValue
                          placeholder={
                            liveModels.length ? t("selectModel") : t("loadModelsFirst")
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        {liveModels.map((model) => (
                          <SelectItem key={model} value={model}>
                            {model}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                  {!spec.custom_model && entry.model && !liveModels.length && (
                    <FieldDescription>
                      {t("savedModelNeedsRefresh", { model: entry.model })}
                    </FieldDescription>
                  )}
                </Field>

                {spec.base_url_configurable && (
                  <Field>
                    <FieldLabel htmlFor={`${spec.id}-url`}>{t("baseUrl")}</FieldLabel>
                    <Input
                      id={`${spec.id}-url`}
                      value={entry.base_url ?? ""}
                      onChange={(event) =>
                        update(spec.id, { base_url: event.target.value || null })
                      }
                      placeholder="https://provider.example/v1"
                    />
                    <FieldDescription>{t("baseUrlDescription")}</FieldDescription>
                  </Field>
                )}

                <Field>
                  <FieldLabel htmlFor={`${spec.id}-credential`}>
                    {spec.credential_label}
                  </FieldLabel>
                  <div className="flex gap-2">
                    <Input
                      id={`${spec.id}-credential`}
                      type={credentialVisible ? "text" : "password"}
                      autoComplete="off"
                      value={entry.credential ?? ""}
                      onChange={(event) => {
                        update(spec.id, {
                          credential: event.target.value,
                          model: spec.custom_model ? entry.model : "",
                          model_verified: false,
                        });
                        if (discoverable) {
                          setModels((current) => ({ ...current, [discoverable]: [] }));
                          setDiscoveryErrors((current) => ({
                            ...current,
                            [discoverable]: undefined,
                          }));
                        }
                      }}
                      placeholder={
                        entry.credential_set_hint
                          ? t("credentialConfigured")
                          : spec.credential_placeholder
                      }
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      aria-label={
                        credentialVisible ? t("hideCredential") : t("showCredential")
                      }
                      onClick={() => toggleVisible(spec.id)}
                    >
                      {credentialVisible ? <EyeOff /> : <Eye />}
                    </Button>
                  </div>
                  <FieldDescription>
                    {entry.credential_set_hint
                      ? t(
                          spec.id === "custom"
                            ? "customCredentialKeepDescription"
                            : "credentialKeepDescription",
                        )
                      : t("credentialDescription")}
                  </FieldDescription>
                  {discoverable && (
                    <Button
                      type="button"
                      variant="outline"
                      className="mt-2 w-full"
                      disabled={
                        modelsLoading ||
                        (!entry.credential?.trim() && !entry.credential_set_hint)
                      }
                      onClick={() => void discoverModels(discoverable)}
                    >
                      {modelsLoading ? (
                        <Spinner />
                      ) : (
                        <RotateCw />
                      )}
                      {modelsLoading ? t("loadingModels") : t("verifyAndLoadModels")}
                    </Button>
                  )}
                  {discoveryError && (
                    <p className="text-xs text-destructive">{discoveryError}</p>
                  )}
                </Field>
              </div>
            );
          })}
        </div>
      </SettingsSection>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
        <p className="text-sm text-muted-foreground">{t("saveHint")}</p>
        <Button type="button" onClick={() => void save()} disabled={saving}>
          {saving ? <Spinner /> : <Save />}
          {saving ? t("saving") : t("save")}
        </Button>
      </div>
    </div>
  );
}
