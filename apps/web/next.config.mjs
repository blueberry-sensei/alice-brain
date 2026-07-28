import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  // Keep development HMR artifacts isolated from `next build` output.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  eslint: { ignoreDuringBuilds: true },
  async redirects() {
    // The v0.3 client shape: old routes -> the new IA
    return [
      { source: "/overview", destination: "/search", permanent: false },
      { source: "/assistants", destination: "/search", permanent: false },
      { source: "/assistants/:id", destination: "/search", permanent: false },
      { source: "/chat", destination: "/search", permanent: false },
      { source: "/chat/:path*", destination: "/search", permanent: false },
      { source: "/sources", destination: "/knowledge", permanent: false },
      { source: "/sources/:id", destination: "/knowledge/:id", permanent: false },
    ];
  },
};

export default withNextIntl(nextConfig);
