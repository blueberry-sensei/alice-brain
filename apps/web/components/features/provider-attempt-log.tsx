"use client";

import * as React from "react";
import { ArrowRightLeft, Ban, Check, RotateCw, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { ProviderAttempt } from "@/lib/types";
import { cn } from "@/lib/utils";

const ACTION_ICON = {
  ok: Check,
  retry: RotateCw,
  failover: ArrowRightLeft,
  abort: Ban,
} as const;

/** Server có thể thêm loại lỗi mới; loại chưa biết rơi về "unknown" thay vì làm vỡ i18n. */
const FAILURE_KINDS = [
  "transient",
  "rate_limit",
  "auth",
  "model_missing",
  "bad_request",
  "unknown",
] as const;

type FailureKind = (typeof FAILURE_KINDS)[number];

function failureKindOf(kind: string): FailureKind {
  return (FAILURE_KINDS as readonly string[]).includes(kind) ? (kind as FailureKind) : "unknown";
}

function formatTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString();
}

/**
 * Lịch sử gọi provider. Đây là câu trả lời cho "vì sao câu trả lời đến từ nhà khác" —
 * mỗi lần thất bại đều có mặt ở đây kèm loại lỗi và nguyên văn lỗi, không có gì bị nuốt.
 */
export function ProviderAttemptLog({
  attempts,
  onRefresh,
}: {
  attempts: ProviderAttempt[];
  onRefresh: () => void;
}) {
  const t = useTranslations("ModelConfig");

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-muted-foreground text-sm">
          {attempts.length === 0 ? t("attemptsEmpty") : t("attemptsCount", { count: attempts.length })}
        </span>
        <Button type="button" variant="ghost" size="sm" onClick={onRefresh}>
          <RotateCw />
          {t("refresh")}
        </Button>
      </div>

      {attempts.length > 0 && (
        <ScrollArea className="h-64 rounded-md border">
          <ul className="divide-y">
            {attempts.map((attempt, index) => {
              const Icon = ACTION_ICON[attempt.action] ?? X;
              return (
                <li key={`${attempt.at}-${attempt.provider_id}-${index}`} className="flex gap-3 p-2.5">
                  <Icon
                    className={cn(
                      "mt-0.5 size-4 shrink-0",
                      attempt.ok ? "text-emerald-600 dark:text-emerald-400" : "text-destructive",
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                      <span className="font-medium">{attempt.label}</span>
                      <Badge variant="outline" className="text-xs">
                        {t(`stage.${attempt.stage}`)}
                      </Badge>
                      {attempt.kind && (
                        <Badge variant="secondary" className="text-xs">
                          {t(`failureKind.${failureKindOf(attempt.kind)}`)}
                        </Badge>
                      )}
                      <span className="text-muted-foreground text-xs">
                        {formatTime(attempt.at)} · {attempt.latency_ms}ms
                        {attempt.attempt > 1 ? ` · #${attempt.attempt}` : ""}
                      </span>
                    </div>
                    {attempt.error && (
                      <p className="text-muted-foreground mt-1 font-mono text-xs break-all">
                        {attempt.error}
                      </p>
                    )}
                  </div>
                  <span className="text-muted-foreground shrink-0 self-start text-xs">
                    {t(`action.${attempt.action}`)}
                  </span>
                </li>
              );
            })}
          </ul>
        </ScrollArea>
      )}
    </div>
  );
}
