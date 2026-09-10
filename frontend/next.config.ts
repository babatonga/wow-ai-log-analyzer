import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

// Note on /api/* routing: in production with an HTTPS reverse proxy in
// front (Caddy/nginx/Traefik), the proxy intercepts /api/* before it
// reaches this Next.js server. Without a proxy (npm run dev, direct
// localhost:3000 access), `src/app/api/[...path]/route.ts` forwards the
// request to BACKEND_URL at request time — so the env var resolves at
// container start rather than image-build time.

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  // Stable since Next 15.5 (was experimental.typedRoutes).
  typedRoutes: true,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default withNextIntl(nextConfig);
