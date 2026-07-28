"use client";

import * as React from "react";
import { Bot, Coins, Database, RotateCw, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { SettingsSection } from "@/components/features/settings-section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api } from "@/lib/api";
import { readClientLocale } from "@/i18n/client";
import { formatTokenCount } from "@/lib/format";
import type {
  TelemetryAgentEvent,
  TelemetryBucket,
  TelemetryLLMCall,
  TelemetrySummary,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const RANGES = [1, 7, 30] as const;
const ROW_LIMIT = 50;
/** Server có thể thêm stage mới; stage lạ hiển thị nguyên văn thay vì làm vỡ i18n. */
const KNOWN_STAGES = ["extraction", "generation", "embedding", "probe"] as const;

/** Chi phí nhỏ tới mức 4 chữ số sau dấu phẩy vẫn ra 0 — hiển thị 6 chữ số cho khỏi thành "0". */
function formatCost(value: number, locale: string): string {
  if (!Number.isFinite(value) || value <= 0) return "$0";
  const digits = value < 0.01 ? 6 : 2;
  return `$${new Intl.NumberFormat(locale, {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  }).format(value)}`;
}

function useStageLabel() {
  const t = useTranslations("Telemetry");
  return React.useCallback(
    (stage: string) => {
      const known = KNOWN_STAGES.find((candidate) => candidate === stage);
      return known ? t(`stage.${known}`) : stage;
    },
    [t],
  );
}

function formatTime(iso: string, locale: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(locale);
}

function StatCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border bg-card p-3">
      <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
        <Icon className="size-3.5" />
        {label}
      </span>
      <span className="truncate text-lg font-semibold">{value}</span>
      {hint && <span className="text-muted-foreground truncate text-xs">{hint}</span>}
    </div>
  );
}

function BucketTable({
  rows,
  labelOf,
  locale,
  emptyLabel,
  headers,
}: {
  rows: TelemetryBucket[];
  labelOf: (row: TelemetryBucket) => string;
  locale: string;
  emptyLabel: string;
  headers: { name: string; calls: string; tokens: string; cost: string };
}) {
  if (rows.length === 0) {
    return <p className="text-muted-foreground p-4 text-sm">{emptyLabel}</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-muted-foreground border-b text-xs">
          <tr>
            <th className="px-4 py-2 text-left font-medium">{headers.name}</th>
            <th className="px-2 py-2 text-right font-medium">{headers.calls}</th>
            <th className="px-2 py-2 text-right font-medium">{headers.tokens}</th>
            <th className="px-4 py-2 text-right font-medium">{headers.cost}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.key}-${row.provider ?? ""}`} className="border-b last:border-b-0">
              <td className="max-w-[16rem] truncate px-4 py-2">{labelOf(row)}</td>
              <td className="px-2 py-2 text-right tabular-nums">{row.calls}</td>
              <td className="px-2 py-2 text-right tabular-nums">
                {formatTokenCount(row.total_tokens, locale)}
              </td>
              <td className="px-4 py-2 text-right tabular-nums">
                {row.unpriced_calls > 0 && row.cost_usd === 0
                  ? "—"
                  : formatCost(row.cost_usd, locale)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CallList({ calls, locale }: { calls: TelemetryLLMCall[]; locale: string }) {
  const t = useTranslations("Telemetry");
  const stageLabel = useStageLabel();
  if (calls.length === 0) {
    return <p className="text-muted-foreground p-4 text-sm">{t("noCalls")}</p>;
  }
  return (
    <ScrollArea className="h-80">
      <ul className="divide-y">
        {calls.map((call) => (
          <li key={call.id} className="flex flex-col gap-1 p-3">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <span
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  call.ok ? "bg-emerald-500" : "bg-destructive",
                )}
                aria-hidden="true"
              />
              <span className="font-medium">{call.model || "(unknown)"}</span>
              <Badge variant="outline" className="text-xs">
                {stageLabel(call.stage)}
              </Badge>
              {call.actor && (
                <Badge variant="secondary" className="text-xs">
                  {call.actor}
                </Badge>
              )}
              <span className="text-muted-foreground text-xs">
                {formatTime(call.at, locale)} · {call.latency_ms}ms
              </span>
            </div>
            <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs tabular-nums">
              <span>
                {t("tokensInOut", {
                  input: formatTokenCount(call.input_tokens, locale),
                  output: formatTokenCount(call.output_tokens, locale),
                })}
              </span>
              <span>
                {call.cost_usd === null ? t("costUnknown") : formatCost(call.cost_usd, locale)}
              </span>
              {call.document_id && <span>doc={call.document_id.slice(0, 8)}</span>}
            </div>
            {call.error && (
              <p className="text-muted-foreground font-mono text-xs break-all">{call.error}</p>
            )}
          </li>
        ))}
      </ul>
    </ScrollArea>
  );
}

function AgentEventList({
  events,
  locale,
}: {
  events: TelemetryAgentEvent[];
  locale: string;
}) {
  const t = useTranslations("Telemetry");
  if (events.length === 0) {
    return <p className="text-muted-foreground p-4 text-sm">{t("noEvents")}</p>;
  }
  return (
    <ScrollArea className="h-80">
      <ul className="divide-y">
        {events.map((event) => {
          const detail = event.detail as { preview?: string; status?: string; note?: string };
          const kind =
            event.kind === "delegation"
              ? "delegation"
              : event.kind === "sub_agent_registry"
                ? "registry"
                : "knowledge";
          return (
            <li key={event.id} className="flex flex-col gap-1 p-3">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <Badge
                  variant={
                    event.kind === "delegation"
                      ? "default"
                      : event.kind === "sub_agent_registry"
                        ? "secondary"
                        : "outline"
                  }
                  className="text-xs"
                >
                  {t(`kind.${kind}`)}
                </Badge>
                <span className="font-medium">{event.tool || "—"}</span>
                <span className="text-muted-foreground text-xs">
                  {event.actor} · {event.transport} · {formatTime(event.at, locale)} ·{" "}
                  {event.latency_ms}ms
                </span>
              </div>
              {event.query && <p className="text-sm break-words">{event.query}</p>}
              <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
                {event.kind === "knowledge_call" ? (
                  <span>
                    {t("resultSummary", {
                      count: event.result_count,
                      chars: event.result_chars,
                    })}
                  </span>
                ) : (
                  <>
                    {detail.status && <span>{detail.status}</span>}
                    {event.model && <span>{event.model}</span>}
                  </>
                )}
              </div>
              {(detail.preview || detail.note) && (
                <p className="text-muted-foreground line-clamp-3 font-mono text-xs break-words">
                  {detail.note || detail.preview}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </ScrollArea>
  );
}

/**
 * Telemetry: tiền và tri thức đi đâu.
 *
 * Hai câu hỏi trang này trả lời: **tinh luyện tốn bao nhiêu** (token + chi phí từng lời gọi
 * LLM, chia theo stage/model/ngày), và **agent đã lấy tri thức gì qua brain** (mỗi lần gọi
 * tool MCP, cùng những lần agent tự khai đã giao việc cho sub-agent).
 */
export function TelemetryPanel() {
  const t = useTranslations("Telemetry");
  const stageLabel = useStageLabel();
  const locale = readClientLocale();
  const [days, setDays] = React.useState<number>(7);
  const [summary, setSummary] = React.useState<TelemetrySummary | null>(null);
  const [calls, setCalls] = React.useState<TelemetryLLMCall[]>([]);
  const [events, setEvents] = React.useState<TelemetryAgentEvent[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [nextSummary, nextCalls, nextEvents] = await Promise.all([
        api.telemetrySummary(days),
        api.telemetryCalls({ limit: ROW_LIMIT }),
        api.telemetryAgentEvents({ limit: ROW_LIMIT }),
      ]);
      setSummary(nextSummary);
      setCalls(nextCalls.items);
      setEvents(nextEvents.items);
      setError(null);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, [days]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const purge = React.useCallback(async () => {
    try {
      const removed = await api.purgeTelemetry();
      toast.success(t("purged", { count: removed.llm_calls + removed.agent_events }));
      await load();
    } catch (purgeError) {
      toast.error(purgeError instanceof Error ? purgeError.message : String(purgeError));
    }
  }, [load, t]);

  const totals = summary?.totals;
  const knowledgeCalls =
    summary?.agent.by_kind.find((row) => row.key === "knowledge_call")?.count ?? 0;
  const delegations = summary?.agent.by_kind.find((row) => row.key === "delegation")?.count ?? 0;

  return (
    <div className="flex flex-col gap-5">
      <SettingsSection title={t("title")} description={t("description")}>
        <div className="flex flex-col gap-4 p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <ToggleGroup
              type="single"
              value={String(days)}
              onValueChange={(value) => value && setDays(Number(value))}
              variant="outline"
              size="sm"
            >
              {RANGES.map((range) => (
                <ToggleGroupItem key={range} value={String(range)}>
                  {t("range", { days: range })}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <div className="flex items-center gap-2">
              <Button type="button" variant="ghost" size="sm" onClick={() => void load()}>
                <RotateCw />
                {t("refresh")}
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => void purge()}>
                <Trash2 />
                {t("clear")}
              </Button>
            </div>
          </div>

          {error && <p className="text-destructive text-sm">{error}</p>}

          {loading && !summary ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {[0, 1, 2, 3].map((index) => (
                <Skeleton key={index} className="h-20 w-full" />
              ))}
            </div>
          ) : (
            totals && (
              <>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <StatCard
                    icon={Coins}
                    label={t("totalCost")}
                    value={formatCost(totals.cost_usd, locale)}
                    hint={
                      totals.unpriced_calls > 0
                        ? t("unpricedHint", { count: totals.unpriced_calls })
                        : undefined
                    }
                  />
                  <StatCard
                    icon={Database}
                    label={t("totalTokens")}
                    value={formatTokenCount(totals.total_tokens, locale)}
                    hint={t("tokensInOut", {
                      input: formatTokenCount(totals.input_tokens, locale),
                      output: formatTokenCount(totals.output_tokens, locale),
                    })}
                  />
                  <StatCard
                    icon={RotateCw}
                    label={t("totalCalls")}
                    value={String(totals.calls)}
                    hint={t("failedHint", { count: totals.failed_calls })}
                  />
                  <StatCard
                    icon={Bot}
                    label={t("agentActivity")}
                    value={String(knowledgeCalls)}
                    hint={t("delegationHint", { count: delegations })}
                  />
                </div>
                {summary && !summary.enabled && (
                  <p className="text-muted-foreground text-sm">{t("disabled")}</p>
                )}
                <p className="text-muted-foreground text-xs">
                  {t("retention", { days: summary?.retention_days ?? 30 })}
                </p>
              </>
            )
          )}
        </div>
      </SettingsSection>

      <SettingsSection title={t("byStage")} description={t("byStageHint")}>
        <BucketTable
          rows={summary?.by_stage ?? []}
          labelOf={(row) => stageLabel(row.key)}
          locale={locale}
          emptyLabel={t("noCalls")}
          headers={{
            name: t("columnStage"),
            calls: t("columnCalls"),
            tokens: t("columnTokens"),
            cost: t("columnCost"),
          }}
        />
      </SettingsSection>

      <SettingsSection title={t("byModel")} description={t("byModelHint")}>
        <BucketTable
          rows={summary?.by_model ?? []}
          labelOf={(row) => (row.provider ? `${row.key} · ${row.provider}` : row.key)}
          locale={locale}
          emptyLabel={t("noCalls")}
          headers={{
            name: t("columnModel"),
            calls: t("columnCalls"),
            tokens: t("columnTokens"),
            cost: t("columnCost"),
          }}
        />
      </SettingsSection>

      <SettingsSection title={t("recent")} description={t("recentHint")}>
        <Tabs defaultValue="calls">
          <TabsList className="m-3">
            <TabsTrigger value="calls">{t("tabCalls")}</TabsTrigger>
            <TabsTrigger value="agent">{t("tabAgent")}</TabsTrigger>
          </TabsList>
          <TabsContent value="calls" className="mt-0">
            <CallList calls={calls} locale={locale} />
          </TabsContent>
          <TabsContent value="agent" className="mt-0">
            <AgentEventList events={events} locale={locale} />
          </TabsContent>
        </Tabs>
      </SettingsSection>
    </div>
  );
}
