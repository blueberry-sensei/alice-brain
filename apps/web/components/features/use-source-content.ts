"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { api, ApiError } from "@/lib/api";
import type { Doc, Source } from "@/lib/types";

const PROCESSING_DOCUMENT_STATES = new Set(["pending", "loading", "extracting"]);

/**
 * Shared source-detail controller for the normal page and the mini workspace.
 * Presentation stays independent while fetching, polling and failure semantics
 * remain identical in both panel shapes.
 */
export function useSourceContent(sourceId: string, active = true) {
  const t = useTranslations("Knowledge");
  const [source, setSource] = React.useState<Source | null>(null);
  const [documents, setDocuments] = React.useState<Doc[] | null>(null);
  const [error, setError] = React.useState("");
  const [notFound, setNotFound] = React.useState(false);
  const [refreshing, setRefreshing] = React.useState(false);

  const refresh = React.useCallback(async () => {
    if (!sourceId) return;
    setRefreshing(true);
    try {
      const [nextSource, nextDocuments] = await Promise.all([
        api.getSource(sourceId),
        api.listDocuments(sourceId),
      ]);
      setSource(nextSource);
      setDocuments(nextDocuments);
      setError("");
      setNotFound(false);
    } catch (reason) {
      const missing = reason instanceof ApiError && reason.status === 404;
      setNotFound(missing);
      setError(
        missing
          ? t("sourceGone")
          : reason instanceof ApiError
            ? reason.message
            : t("sourceContentFailed"),
      );
    } finally {
      setRefreshing(false);
    }
  }, [sourceId, t]);

  React.useEffect(() => {
    setSource(null);
    setDocuments(null);
    setError("");
    setNotFound(false);
    if (active) void refresh();
  }, [active, refresh, sourceId]);

  const processing =
    documents?.some((document) => PROCESSING_DOCUMENT_STATES.has(document.status)) ?? false;

  React.useEffect(() => {
    if (!active || !processing) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [active, processing, refresh]);

  return {
    source,
    documents,
    error,
    notFound,
    refreshing,
    processing,
    refresh,
  };
}
