"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";

import { api } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { cn } from "@/lib/utils";

// Attachments need a Bearer request: blob URLs are cached per session (there are few images, so nothing is reclaimed eagerly)
const cache = new Map<string, string>();

/** Authenticated image - a local preview renders the url directly; a server attachment is fetched by id with Bearer as a blob. */
export function AuthImage({
  id,
  url,
  alt,
  className,
}: {
  id?: string;
  url?: string;
  alt?: string;
  className?: string;
}) {
  const t = useTranslations("Markdown");
  const locale = useLocale();
  const [src, setSrc] = React.useState<string | null>(url ?? (id ? cache.get(id) ?? null : null));

  React.useEffect(() => {
    if (url) {
      setSrc(url);
      return;
    }
    if (!id) return;
    const hit = cache.get(id);
    if (hit) {
      setSrc(hit);
      return;
    }
    let alive = true;
    fetch(api.attachmentUrl(id), {
      headers: {
        Authorization: `Bearer ${getToken() ?? ""}`,
        "Accept-Language": locale,
      },
    })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((b) => {
        const obj = URL.createObjectURL(b);
        cache.set(id, obj);
        if (alive) setSrc(obj);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [id, locale, url]);

  if (!src) return <div className={cn("animate-pulse rounded-md bg-muted", className)} />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={alt ?? t("image")} className={className} />;
}
