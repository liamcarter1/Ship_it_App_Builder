/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Proxy `/api/*` to the FastAPI backend in dev so the dashboard can use
  // same-origin URLs (no CORS surprises) regardless of where the backend
  // lives. Override the target with NEXT_PUBLIC_BACKEND_URL.
  async rewrites() {
    const backend = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://127.0.0.1:8000';
    return [{ source: '/api/:path*', destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
