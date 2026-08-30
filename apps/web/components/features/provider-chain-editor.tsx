"use client";

import * as React from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  Clock,
  Plug,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Alert, AlertDescription } from "@/components/ui/alert";
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
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api, ApiError } from "@/lib/api";
import type {
  LLMProviderEntryInput,
  ModelProviderSpec,
  ProviderHealth,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** Trạng thái sức khoẻ hiển thị trên đầu mỗi card. */
type HealthState = "ok" | "cooldown" | "disabled" | "unknown";

function healthOf(health: ProviderHealth | undefined): HealthState {
  if (!health) return "unknown";
  if (health.unhealthy_reason) return "disabled";
  if (health.cooldown_remaining > 0) return "cooldown";
  return "ok";
}

function newEntryId(existing: LLMProviderEntryInput[]): string {
  for (let index = 1; index < 100; index += 1) {
    const candidate = `provider-${index}`;
    if (!existing.some((entry) => entry.id === candidate)) return candidate;
  }
  return `provider-${Date.now()}`;
}

export function ProviderChainEditor({
  entries,
  providers,
  health,
  onChange,
}: {
  entries: LLMProviderEntryInput[];
  providers: ModelProviderSpec[];
  health: ProviderHealth[];
  onChange: (next: LLMProviderEntryInput[]) => void;
}) {
  const t = useTranslations("ModelConfig");
  const [openId, setOpenId] = React.useState<string | null>(entries[0]?.id ?? null);
  const [testingId, setTestingId] = React.useState<string | null>(null);
  const [testResults, setTestResults] = React.useState<
    Record<string, { ok: boolean; message: string }>
  >({});
  // Bản nháp JSON của extra_body: giữ nguyên chữ người dùng đang gõ để họ sửa được
  // JSON hỏng, thay vì bị component ghi đè mỗi lần re-render.
  const [extraDrafts, setExtraDrafts] = React.useState<Record<string, string>>({});

  const healthById = React.useMemo(() => {
    const map = new Map<string, ProviderHealth>();
    for (const item of health) map.set(item.provider_id, item);
    return map;
  }, [health]);

  const sorted = React.useMemo(
    () => [...entries].sort((a, b) => a.priority - b.priority),
    [entries],
  );

  function update(id: string, patch: Partial<LLMProviderEntryInput>) {
    onChange(entries.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry)));
  }

  function remove(id: string) {
    onChange(entries.filter((entry) => entry.id !== id));
    setTestResults((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
  }

  /** Đổi chỗ hai entry liền kề rồi **đánh số lại** priority theo bậc 10 cho dễ đọc. */
  function move(id: string, direction: -1 | 1) {
    const order = sorted.map((entry) => entry.id);
    const from = order.indexOf(id);
    const to = from + direction;
    if (from < 0 || to < 0 || to >= order.length) return;
    [order[from], order[to]] = [order[to], order[from]];
    const priorityById = new Map(order.map((entryId, index) => [entryId, (index + 1) * 10]));
    onChange(entries.map((entry) => ({ ...entry, priority: priorityById.get(entry.id)! })));
  }

  function add() {
    const spec = providers[0];
    const id = newEntryId(entries);
    const next: LLMProviderEntryInput = {
      id,
      provider: spec.id,
      model: spec.default_model,
      label: "",
      base_url: spec.default_base_url,
      priority: (entries.length + 1) * 10,
      enabled: true,
      extra_body: null,
      cooldown_seconds: 60,
      temperature: null,
      max_tokens: null,
      timeout_ms: null,
      max_retries: null,
      api_key: "",
    };
    onChange([...entries, next]);
    setOpenId(id);
  }

  async function test(entry: LLMProviderEntryInput) {
    setTestingId(entry.id);
    try {
      const result = await api.testModelConfig(entry);
      setTestResults((previous) => ({ ...previous, [entry.id]: result }));
    } catch (error) {
      setTestResults((previous) => ({
        ...previous,
        [entry.id]: {
          ok: false,
          message: error instanceof ApiError ? error.message : t("testFailed"),
        },
      }));
    } finally {
      setTestingId(null);
    }
  }

  if (entries.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        <Alert>
          <AlertTriangle />
          <AlertDescription>{t("chainEmpty")}</AlertDescription>
        </Alert>
        <Button type="button" variant="outline" size="sm" className="self-start" onClick={add}>
          <Plus />
          {t("addProvider")}
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground text-sm">{t("chainHint")}</p>

      {sorted.map((entry, index) => {
        const spec = providers.find((provider) => provider.id === entry.provider) ?? providers[0];
        const state = healthOf(healthById.get(entry.id));
        const entryHealth = healthById.get(entry.id);
        const open = openId === entry.id;
        const result = testResults[entry.id];
        const extraDraft =
          extraDrafts[entry.id] ??
          (entry.extra_body ? JSON.stringify(entry.extra_body, null, 2) : "");
        let extraError: string | null = null;
        if (extraDraft.trim()) {
          try {
            const parsed = JSON.parse(extraDraft);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
              extraError = t("extraBodyInvalid");
            }
          } catch {
            extraError = t("extraBodyInvalid");
          }
        }

        return (
          <div
            key={entry.id}
            className={cn(
              "rounded-lg border",
              !entry.enabled && "opacity-60",
              state === "disabled" && "border-destructive/50",
            )}
          >
            <div className="flex flex-wrap items-center gap-2 p-3">
              <Badge variant="outline" className="font-mono">
                #{index + 1}
              </Badge>
              <div className="flex flex-col gap-0.5">
                <span className="text-sm font-medium">
                  {entry.label?.trim() || `${spec.display_name} · ${entry.model || "—"}`}
                </span>
                <span className="text-muted-foreground text-xs">
                  {spec.display_name} · {entry.model || t("modelMissing")}
                </span>
              </div>

              {state === "disabled" && (
                <Badge variant="destructive" className="gap-1">
                  <AlertTriangle className="size-3" />
                  {t("healthDisabled")}
                </Badge>
              )}
              {state === "cooldown" && (
                <Badge variant="secondary" className="gap-1">
                  <Clock className="size-3" />
                  {/* Nghỉ do `Retry-After` của server có thể dài hàng chục phút; "1800s" là con
                      số không ai đọc được, nên đổi sang phút ngay khi vượt một phút rưỡi. */}
                  {(entryHealth?.cooldown_remaining ?? 0) > 90
                    ? t("healthCooldownMinutes", {
                        minutes: Math.ceil((entryHealth?.cooldown_remaining ?? 0) / 60),
                      })
                    : t("healthCooldown", {
                        seconds: Math.ceil(entryHealth?.cooldown_remaining ?? 0),
                      })}
                </Badge>
              )}

              <div className="ms-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t("moveUp")}
                  disabled={index === 0}
                  onClick={() => move(entry.id, -1)}
                >
                  <ArrowUp />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t("moveDown")}
                  disabled={index === sorted.length - 1}
                  onClick={() => move(entry.id, 1)}
                >
                  <ArrowDown />
                </Button>
                <Switch
                  checked={entry.enabled}
                  aria-label={t("enabledLabel")}
                  onCheckedChange={(checked) => update(entry.id, { enabled: checked })}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label={t("expand")}
                  onClick={() => setOpenId(open ? null : entry.id)}
                >
                  <ChevronDown className={cn("transition-transform", open && "rotate-180")} />
                </Button>
              </div>
            </div>

            {state === "disabled" && entryHealth?.unhealthy_reason && (
              <p className="text-destructive px-3 pb-2 text-xs">
                {entryHealth.unhealthy_reason}
              </p>
            )}

            {open && (
              <div className="grid gap-4 border-t p-3 sm:grid-cols-2">
                <Field>
                  <FieldLabel htmlFor={`${entry.id}-provider`}>{t("provider")}</FieldLabel>
                  <Select
                    value={entry.provider}
                    onValueChange={(value) => {
                      const next = providers.find((provider) => provider.id === value);
                      if (!next) return;
                      const knownModels = new Set(providers.map((p) => p.default_model));
                      const knownUrls = new Set(
                        providers.map((p) => p.default_base_url).filter(Boolean),
                      );
                      update(entry.id, {
                        provider: next.id,
                        // Chỉ thay khi người dùng chưa tự điền: đừng xoá chữ họ gõ.
                        model:
                          !entry.model.trim() || knownModels.has(entry.model.trim())
                            ? next.default_model
                            : entry.model,
                        base_url:
                          !entry.base_url?.trim() || knownUrls.has(entry.base_url.trim())
                            ? next.default_base_url
                            : entry.base_url,
                      });
                    }}
                  >
                    <SelectTrigger id={`${entry.id}-provider`}>
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
                  <FieldDescription>{t(`providerDescription.${entry.provider}`)}</FieldDescription>
                </Field>

                <Field>
                  <FieldLabel htmlFor={`${entry.id}-model`}>{t("model")}</FieldLabel>
                  <Input
                    id={`${entry.id}-model`}
                    value={entry.model}
                    onChange={(event) => update(entry.id, { model: event.target.value })}
                    placeholder={spec.default_model || "model-name"}
                  />
                </Field>

                <Field>
                  <FieldLabel htmlFor={`${entry.id}-url`}>Base URL</FieldLabel>
                  <Input
                    id={`${entry.id}-url`}
                    value={entry.base_url ?? ""}
                    onChange={(event) => update(entry.id, { base_url: event.target.value || null })}
                    placeholder={spec.default_base_url ?? t("officialEndpoint")}
                  />
                  <FieldDescription>{t(`baseUrlDescription.${entry.provider}`)}</FieldDescription>
                </Field>

                <Field>
                  <FieldLabel htmlFor={`${entry.id}-key`}>API Key</FieldLabel>
                  <Input
                    id={`${entry.id}-key`}
                    type="password"
                    autoComplete="off"
                    value={entry.api_key ?? ""}
                    onChange={(event) => update(entry.id, { api_key: event.target.value })}
                    placeholder={
                      entry.api_key_set_hint ? t("keyConfigured") : spec.api_key_placeholder
                    }
                  />
                  <FieldDescription>
                    {entry.api_key_set_hint ? t("keyKeepDescription") : t("secretDescription")}
                  </FieldDescription>
                </Field>

                <Field>
                  <FieldLabel htmlFor={`${entry.id}-label`}>{t("labelField")}</FieldLabel>
                  <Input
                    id={`${entry.id}-label`}
                    value={entry.label}
                    onChange={(event) => update(entry.id, { label: event.target.value })}
                    placeholder={`${spec.display_name} · ${entry.model || "model"}`}
                  />
                  <FieldDescription>{t("labelDescription")}</FieldDescription>
                </Field>

                <Field>
                  <FieldLabel htmlFor={`${entry.id}-cooldown`}>{t("cooldown")}</FieldLabel>
                  <Input
                    id={`${entry.id}-cooldown`}
                    type="number"
                    min={0}
                    max={3600}
                    value={entry.cooldown_seconds}
                    onChange={(event) =>
                      update(entry.id, {
                        cooldown_seconds: Math.max(0, Number(event.target.value) || 0),
                      })
                    }
                  />
                  <FieldDescription>{t("cooldownDescription")}</FieldDescription>
                </Field>

                <Field className="sm:col-span-2">
                  <FieldLabel htmlFor={`${entry.id}-extra`}>{t("extraBody")}</FieldLabel>
                  <Textarea
                    id={`${entry.id}-extra`}
                    rows={3}
                    className="font-mono text-xs"
                    value={extraDraft}
                    onChange={(event) => {
                      const text = event.target.value;
                      setExtraDrafts((previous) => ({ ...previous, [entry.id]: text }));
                      if (!text.trim()) {
                        update(entry.id, { extra_body: null });
                        return;
                      }
                      try {
                        const parsed = JSON.parse(text);
                        if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
                          update(entry.id, { extra_body: parsed as Record<string, unknown> });
                        }
                      } catch {
                        // JSON chưa hoàn chỉnh trong lúc gõ — không ghi vào state, chỉ báo lỗi.
                      }
                    }}
                    placeholder='{"provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": false}}'
                  />
                  <FieldDescription className={cn(extraError && "text-destructive")}>
                    {extraError ?? t("extraBodyDescription")}
                  </FieldDescription>
                </Field>

                <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={testingId === entry.id || !entry.model.trim()}
                    onClick={() => void test(entry)}
                  >
                    {testingId === entry.id ? <Spinner /> : <Plug />}
                    {t("test")}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="text-destructive"
                    onClick={() => remove(entry.id)}
                  >
                    <Trash2 />
                    {t("removeProvider")}
                  </Button>
                  {result && (
                    <span
                      className={cn(
                        "flex items-center gap-1 text-sm",
                        result.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive",
                      )}
                    >
                      {result.ok ? <Check className="size-4" /> : <X className="size-4" />}
                      {result.message}
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}

      <Button type="button" variant="outline" size="sm" className="self-start" onClick={add}>
        <Plus />
        {t("addProvider")}
      </Button>
    </div>
  );
}
