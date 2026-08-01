"use client";

import * as React from "react";
import { Bot, Coins, Database, Download, RotateCw, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import { SettingsSection } from "@/components/features/settings-section";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api } from "@/lib/api";
import { readClientLocale } from "@/i18n/client";
import { formatTokenCount } from "@/lib/format";
import { datedJsonFilename, downloadJsonFile } from "@/lib/settings-config-transfer";
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

function mergeUnique<T extends { id: string }>(current: T[], incoming: T[]): T[] {
  const seen = new Set(current.map((item) => item.id));
  return [...current, ...incoming.filter((item) => !seen.has(item.id))];
}

async function fetchAll<T>(
  loader: (offset: number, limit: number) => Promise<{ total: number; items: T[] }>,
): Promise<T[]> {
  const items: T[] = [];
  const limit = 200;
  while (true) {
    const page = await loader(items.length, limit);
    items.push(...page.items);
    if (page.items.length === 0 || items.length >= page.total) return items;
  }
}

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

function CallList({
  calls,
  total,
  locale,
  loadingMore,
  onLoadMore,
}: {
  calls: TelemetryLLMCall[];
  total: number;
  locale: string;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const t = useTranslations("Telemetry");
  const stageLabel = useStageLabel();
  if (calls.length === 0) {
    return <p className="text-muted-foreground p-4 text-sm">{t("noCalls")}</p>;
  }
  return (
    <ScrollArea className="h-[32rem] sm:h-[40rem]">
      <ul className="divide-y">
        {calls.map((call) => (
          <li key={call.id} className="flex flex-col gap-2 p-4 transition-colors hover:bg-muted/30">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  call.ok ? "bg-emerald-500" : "bg-destructive",
                )}
                aria-hidden="true"
              />
              <Badge variant="outline" className="text-xs">
                {stageLabel(call.stage)}
              </Badge>
              <Badge variant={call.ok ? "secondary" : "destructive"} className="text-xs">
                {call.ok ? t("succeeded") : t("failed")}
              </Badge>
              {call.actor && (
                <Badge variant="secondary" className="text-xs">
                  {call.actor}
                </Badge>
              )}
              <span className="text-muted-foreground ms-auto text-xs">
                {formatTime(call.at, locale)}
              </span>
            </div>
            <div>
              <p className="font-medium break-words">{call.model || "(unknown)"}</p>
              <p className="text-muted-foreground text-xs">
                {call.provider || "—"} · {call.call_type || "—"}
              </p>
            </div>
            <div className="grid gap-2 text-xs tabular-nums sm:grid-cols-3">
              <span className="rounded-md bg-muted/50 px-2.5 py-2">
                {t("tokensInOut", {
                  input: formatTokenCount(call.input_tokens, locale),
                  output: formatTokenCount(call.output_tokens, locale),
                })}
              </span>
              <span className="rounded-md bg-muted/50 px-2.5 py-2">
                {call.cost_usd === null ? t("costUnknown") : formatCost(call.cost_usd, locale)}
              </span>
              <span className="rounded-md bg-muted/50 px-2.5 py-2">
                {t("latency", { value: call.latency_ms })}
              </span>
            </div>
            {(call.document_id || call.failure_kind) && (
              <p className="text-muted-foreground text-xs break-all">
                {[call.failure_kind && t("failureKind", { value: call.failure_kind }), call.document_id && `doc=${call.document_id}`]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
            {call.error && (
              <p className="text-destructive rounded-md bg-destructive/5 p-2 font-mono text-xs break-all">
                {call.error}
              </p>
            )}
          </li>
        ))}
        <li className="flex flex-wrap items-center justify-between gap-3 p-4">
          <span className="text-muted-foreground text-xs">
            {t("loaded", { shown: calls.length, total })}
          </span>
          {calls.length < total && (
            <Button type="button" variant="outline" size="sm" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore && <Spinner />}
              {loadingMore ? t("loadingMore") : t("loadMore")}
            </Button>
          )}
        </li>
      </ul>
    </ScrollArea>
  );
}

function AgentEventList({
  events,
  total,
  locale,
  loadingMore,
  onLoadMore,
}: {
  events: TelemetryAgentEvent[];
  total: number;
  locale: string;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const t = useTranslations("Telemetry");
  if (events.length === 0) {
    return <p className="text-muted-foreground p-4 text-sm">{t("noEvents")}</p>;
  }
  return (
    <ScrollArea className="h-[32rem] sm:h-[40rem]">
      <ul className="divide-y">
        {events.map((event) => {
          const detail = event.detail as {
            preview?: string;
            status?: string;
            note?: string;
            created_count?: number;
            updated_count?: number;
            deleted_count?: number;
          };
          const kind =
            event.kind === "delegation"
              ? "delegation"
              : event.kind === "sub_agent_registry"
                ? "registry"
                : event.kind === "knowledge_write"
                  ? "write"
                  : "knowledge";
          return (
            <li key={event.id} className="flex flex-col gap-2 p-4 transition-colors hover:bg-muted/30">
              <div className="flex flex-wrap items-center gap-2 text-sm">
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
                <Badge variant={event.ok ? "secondary" : "destructive"} className="text-xs">
                  {event.ok ? t("succeeded") : t("failed")}
                </Badge>
                <span className="text-muted-foreground ms-auto text-xs">
                  {formatTime(event.at, locale)}
                </span>
              </div>
              <p className="text-muted-foreground text-xs">
                {event.actor} · {event.transport} · {t("latency", { value: event.latency_ms })}
              </p>
              {event.query && <p className="rounded-md bg-muted/40 p-2 text-sm break-words">{event.query}</p>}
              <div className="text-muted-foreground flex flex-wrap gap-x-3 text-xs">
                {event.kind === "knowledge_call" ? (
                  <span>
                    {t("resultSummary", {
                      count: event.result_count,
                      chars: event.result_chars,
                    })}
                  </span>
                ) : event.kind === "knowledge_write" ? (
                  <span>
                    {t("writeSummary", {
                      created: detail.created_count ?? 0,
                      updated: detail.updated_count ?? 0,
                      deleted: detail.deleted_count ?? 0,
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
                <p className="text-muted-foreground line-clamp-5 rounded-md border bg-card p-2 font-mono text-xs break-words">
                  {detail.note || detail.preview}
                </p>
              )}
              {event.error && (
                <p className="text-destructive rounded-md bg-destructive/5 p-2 font-mono text-xs break-all">
                  {event.error}
                </p>
              )}
            </li>
          );
        })}
        <li className="flex flex-wrap items-center justify-between gap-3 p-4">
          <span className="text-muted-foreground text-xs">
            {t("loaded", { shown: events.length, total })}
          </span>
          {events.length < total && (
            <Button type="button" variant="outline" size="sm" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore && <Spinner />}
              {loadingMore ? t("loadingMore") : t("loadMore")}
            </Button>
          )}
        </li>
      </ul>
    </ScrollArea>
  );
}

/**
 * Telemetry: tiền và tri thức đi đâu.
 *
 * Hai câu hỏi trang này trả lời: **tinh luyện tốn bao nhiêu** (token + chi phí từng lời gọi
 * LLM, chia theo stage/model/ngày), và **agent đã lấy tri thức gì qua brain** (mỗi lần gọi
 * tool MCP, các diff đã sync vào Brain, cùng những lần giao việc cho sub-agent).
 */
export function TelemetryPanel() {
  const t = useTranslations("Telemetry");
  const stageLabel = useStageLabel();
  const locale = readClientLocale();
  const [days, setDays] = React.useState<number>(7);
  const [summary, setSummary] = React.useState<TelemetrySummary | null>(null);
  const [calls, setCalls] = React.useState<TelemetryLLMCall[]>([]);
  const [events, setEvents] = React.useState<TelemetryAgentEvent[]>([]);
  const [callTotal, setCallTotal] = React.useState(0);
  const [eventTotal, setEventTotal] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [loadingMoreCalls, setLoadingMoreCalls] = React.useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = React.useState(false);
  const [exporting, setExporting] = React.useState(false);
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
      setCallTotal(nextCalls.total);
      setEventTotal(nextEvents.total);
      setError(null);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoading(false);
    }
  }, [days]);

  const loadMoreCalls = React.useCallback(async () => {
    setLoadingMoreCalls(true);
    try {
      const page = await api.telemetryCalls({ limit: ROW_LIMIT, offset: calls.length });
      setCalls((current) => mergeUnique(current, page.items));
      setCallTotal(page.total);
    } catch (loadError) {
      toast.error(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoadingMoreCalls(false);
    }
  }, [calls.length]);

  const loadMoreEvents = React.useCallback(async () => {
    setLoadingMoreEvents(true);
    try {
      const page = await api.telemetryAgentEvents({ limit: ROW_LIMIT, offset: events.length });
      setEvents((current) => mergeUnique(current, page.items));
      setEventTotal(page.total);
    } catch (loadError) {
      toast.error(loadError instanceof Error ? loadError.message : String(loadError));
    } finally {
      setLoadingMoreEvents(false);
    }
  }, [events.length]);

  const exportReport = React.useCallback(async () => {
    setExporting(true);
    try {
      const [reportSummary, allCalls, allEvents] = await Promise.all([
        api.telemetrySummary(days),
        fetchAll((offset, limit) => api.telemetryCalls({ offset, limit })),
        fetchAll((offset, limit) => api.telemetryAgentEvents({ offset, limit })),
      ]);
      const since = new Date(reportSummary.since).getTime();
      const report = {
        kind: "alice-telemetry-report",
        version: 1,
        exported_at: new Date().toISOString(),
        range_days: days,
        summary: reportSummary,
        llm_calls: allCalls.filter((call) => new Date(call.at).getTime() >= since),
        agent_events: allEvents.filter((event) => new Date(event.at).getTime() >= since),
      };
      downloadJsonFile(datedJsonFilename("alice-telemetry-report"), report);
      toast.success(t("exported"));
    } catch (exportError) {
      toast.error(exportError instanceof Error ? exportError.message : t("exportFailed"));
    } finally {
      setExporting(false);
    }
  }, [days, t]);

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
  const knowledgeActivity = (summary?.agent.by_kind ?? [])
    .filter((row) => row.key === "knowledge_call" || row.key === "knowledge_write")
    .reduce((total, row) => total + row.count, 0);
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
              <Button type="button" variant="outline" size="sm" disabled={exporting} onClick={() => void exportReport()}>
                {exporting ? <Spinner /> : <Download />}
                {exporting ? t("exporting") : t("export")}
              </Button>
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
                    value={String(knowledgeActivity)}
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
            <CallList
              calls={calls}
              total={callTotal}
              locale={locale}
              loadingMore={loadingMoreCalls}
              onLoadMore={() => void loadMoreCalls()}
            />
          </TabsContent>
          <TabsContent value="agent" className="mt-0">
            <AgentEventList
              events={events}
              total={eventTotal}
              locale={locale}
              loadingMore={loadingMoreEvents}
              onLoadMore={() => void loadMoreEvents()}
            />
          </TabsContent>
        </Tabs>
      </SettingsSection>
    </div>
  );
}
