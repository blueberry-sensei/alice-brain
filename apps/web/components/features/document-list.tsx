"use client";

import * as React from "react";
import { FileText, Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import type { Doc } from "@/lib/types";
import { formatBytes, formatTokenCount, relativeTime } from "@/lib/format";
import { useDetailPanel } from "@/components/features/detail-panel";
import { useApp } from "@/components/features/app-shell";
import { DocStatusBadge } from "@/components/features/status-badge";
import { Button } from "@/components/ui/button";

export function DocumentList({
  sourceId,
  documents,
  onChange,
  variant = "normal",
  onOpenDocument,
}: {
  sourceId: string;
  documents: Doc[];
  onChange: () => void;
  variant?: "normal" | "compact";
  onOpenDocument?: (document: Doc) => void;
}) {
  const t = useTranslations("DocumentList");
  const locale = useLocale();
  const [pending, setPending] = React.useState<string | null>(null);
  const { open } = useDetailPanel();
  const { timezone } = useApp();

  async function reprocess(d: Doc) {
    setPending(d.id);
    try {
      await api.reprocessDocument(sourceId, d.id);
      toast.success(t("requeued"));
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("operationFailed"));
    } finally {
      setPending(null);
    }
  }

  async function remove(d: Doc) {
    setPending(d.id);
    try {
      await api.deleteDocument(sourceId, d.id);
      toast.success(t("deleted"));
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("deleteFailed"));
    } finally {
      setPending(null);
    }
  }

  async function pause(d: Doc) {
    setPending(d.id);
    try {
      await api.pauseDocument(sourceId, d.id);
      toast.success(t("pausing"));
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("pauseFailed"));
    } finally {
      setPending(null);
    }
  }

  async function resume(d: Doc) {
    setPending(d.id);
    try {
      await api.resumeDocument(sourceId, d.id);
      toast.success(t("resumed"));
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : t("resumeFailed"));
    } finally {
      setPending(null);
    }
  }

  if (variant === "compact") {
    return (
      <div className="space-y-0.5">
        {documents.map((document) => (
          <button
            key={document.id}
            type="button"
            onClick={() => {
              if (onOpenDocument) onOpenDocument(document);
              else open({ kind: "document", sourceId, documentId: document.id });
            }}
            className="group/document flex w-full items-center gap-3 rounded-lg px-2.5 py-2.5 text-left outline-none transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
            title={t("viewDocument")}
          >
            <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground transition-colors group-hover/document:text-foreground">
              <FileText className="size-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-foreground">
                {document.filename}
              </div>
              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                <span>{formatBytes(document.size_bytes, locale)}</span>
                <span>·</span>
                <span>{relativeTime(document.created_at, timezone, locale)}</span>
                {document.status === "ready" && (
                  <>
                    <span>·</span>
                    <span className="truncate">{t("events", { count: document.event_count })}</span>
                  </>
                )}
              </div>
            </div>
            <DocStatusBadge status={document.status} />
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      {documents.map((d, i) => {
        const progress = Math.min(100, Math.max(0, Math.round(d.progress ?? 0)));
        const showProgress =
          d.status === "loading" || d.status === "extracting" || d.status === "paused";
        const showMetrics = showProgress || d.status === "failed";
        return (
          <div
            key={d.id}
            className={
              "flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted/60 " +
              (i > 0 ? "border-t" : "")
            }
          >
            <div className="grid size-9 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground">
              <FileText className="size-4" />
            </div>

            <button
              type="button"
              onClick={() => open({ kind: "document", sourceId, documentId: d.id })}
              className="min-w-0 flex-1 rounded-md text-left outline-none focus-visible:bg-muted/60"
              title={t("viewDetails")}
            >
              <div className="truncate text-sm font-medium text-foreground">{d.filename}</div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                <span>{formatBytes(d.size_bytes, locale)}</span>
                <span>·</span>
                <span>{relativeTime(d.created_at, timezone, locale)}</span>
                {d.status === "ready" && (
                  <>
                    <span>·</span>
                    <span>
                      {t("chunksAndEvents", {
                        chunks: d.chunk_count,
                        events: d.event_count,
                      })}
                    </span>
                    <span>·</span>
                    <span>100% · {t("tokens", { count: formatTokenCount(d.token_usage, locale) })}</span>
                  </>
                )}
                {showMetrics && (
                  <>
                    <span>·</span>
                    <span>{progress}% · {t("tokens", { count: formatTokenCount(d.token_usage, locale) })}</span>
                  </>
                )}
                {d.status === "failed" && d.error && (
                  <>
                    <span>·</span>
                    <span className="truncate text-destructive" title={d.error}>
                      {d.error}
                    </span>
                  </>
                )}
              </div>
              {showProgress && (
                <div className="mt-1.5 h-1 w-full max-w-56 overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-primary/60 transition-[width] duration-300"
                    style={{ width: `${progress}%` }}
                  />
                </div>
              )}
            </button>

            <DocStatusBadge status={d.status} />

            <div className="flex shrink-0 items-center gap-0.5">
              {d.status === "extracting" && (
                <Button
                  variant="ghost"
                  size="icon"
                  title={t("pause")}
                  disabled={pending === d.id}
                  onClick={() => pause(d)}
                >
                  <Pause className="size-4" />
                </Button>
              )}
              {d.status === "paused" && (
                <Button
                  variant="ghost"
                  size="icon"
                  title={t("resume")}
                  disabled={pending === d.id}
                  onClick={() => resume(d)}
                >
                  <Play className="size-4" />
                </Button>
              )}
              {(d.status === "failed" || d.status === "ready") && (
                <Button
                  variant="ghost"
                  size="icon"
                  title={t("reprocess")}
                  disabled={pending === d.id}
                  onClick={() => reprocess(d)}
                >
                  <RefreshCw className="size-4" />
                </Button>
              )}
              <Button
                variant="ghost"
                size="icon"
                title={t("delete")}
                disabled={pending === d.id}
                onClick={() => remove(d)}
                className="text-muted-foreground hover:text-destructive"
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
