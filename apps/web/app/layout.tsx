import "./globals.css";

import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";

import { PRODUCT_NAME } from "@/lib/branding";
import { Providers } from "@/components/providers";
import { localeDocumentTag } from "@/i18n/config";
import { fontVars } from "./fonts";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("Metadata");
  return { title: PRODUCT_NAME, description: t("description") };
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();
  // Cổng API đọc lúc CHẠY, không phải lúc build: cùng một image web được nhiều project dùng,
  // mỗi project một cổng. Lọc còn chữ số nên chuỗi nhúng vào script không thể mang gì khác.
  const apiPort = (process.env.SAG_PUBLIC_API_PORT ?? "").replace(/\D/g, "");
  return (
    <html lang={localeDocumentTag(locale)} suppressHydrationWarning className={fontVars}>
      <head>
        <script
          // Phải chạy TRƯỚC bundle ứng dụng vì lib/api.ts đọc giá trị này ngay lúc nạp module.
          dangerouslySetInnerHTML={{ __html: `window.__ALICE_API_PORT__="${apiPort}";` }}
        />
      </head>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <NextIntlClientProvider locale={locale} messages={messages}>
          <Providers>{children}</Providers>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
