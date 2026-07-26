"use client";

import * as React from "react";

import { normalizeAvatar } from "@/lib/avatar";
import { DEFAULT_AGENT_AVATAR } from "@/lib/branding";
import { cn } from "@/lib/utils";

type AgentAvatarSize = "sm" | "md" | "lg";

const SIZES: Record<AgentAvatarSize, string> = {
  sm: "size-8 rounded-lg",
  md: "size-10 rounded-lg",
  lg: "size-16 rounded-xl",
};

/** Cỡ chữ co theo độ dài nhãn: emoji đơn thì to, chuỗi dài thì nhỏ lại cho vừa ô. */
export function agentAvatarTextStyle(value: string): React.CSSProperties {
  const length = Array.from(value).length;
  const emojiLike = /\p{Extended_Pictographic}/u.test(value);
  if (emojiLike && length <= 2) {
    return { fontFamily: "system-ui, sans-serif", fontSize: length === 1 ? 20 : 16 };
  }
  if (length <= 1) return { fontSize: 16 };
  if (length <= 3) return { fontSize: 13 };
  if (length <= 5) return { fontSize: 10 };
  return { fontSize: 8 };
}

const LG_SCALE = 1.55;

export function AgentAvatar({
  face,
  size = "sm",
  className,
}: {
  face: string;
  size?: AgentAvatarSize;
  className?: string;
}) {
  const value = normalizeAvatar(face) || DEFAULT_AGENT_AVATAR;
  const base = agentAvatarTextStyle(value);
  const style =
    size === "lg" && typeof base.fontSize === "number"
      ? { ...base, fontSize: Math.round(base.fontSize * LG_SCALE) }
      : base;

  return (
    <span
      aria-hidden="true"
      className={cn("alice-agent-avatar shrink-0", SIZES[size], className)}
    >
      <span className="alice-agent-avatar__glyph" style={style}>
        {value}
      </span>
    </span>
  );
}
