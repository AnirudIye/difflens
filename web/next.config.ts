import type { NextConfig } from "next";

/* Cheap, non-breaking hardening. A real Content-Security-Policy is
   deliberately absent: Next's App Router needs either 'unsafe-inline' for its
   own bootstrap scripts or a per-request nonce, and the nonce route forces
   every page to render dynamically, which costs the static prerendering this
   app runs on. docs/THREAT_MODEL.md carries that as an accepted gap rather
   than shipping a policy that says unsafe-inline and calls itself a policy. */
const SECURITY_HEADERS = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Nothing here is meant to be framed, and clickjacking a "Run review"
  // button is the obvious attack
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), interest-cohort=()",
  },
];

const nextConfig: NextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },

  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${process.env.API_ORIGIN ?? "http://localhost:8000"}/:path*`,
      },
    ];
  },
};

export default nextConfig;
