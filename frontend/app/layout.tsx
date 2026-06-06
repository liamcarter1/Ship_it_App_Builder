import './globals.css';
import type { Metadata } from 'next';
import Link from 'next/link';

export const metadata: Metadata = {
  title: 'Ship-It Dashboard',
  description: 'Watch the app factory pipeline run in real time.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen font-mono bg-gradient-to-b from-zinc-950 via-black to-zinc-950 text-zinc-100">
        <header className="sticky top-0 z-30 border-b border-zinc-800/80 bg-zinc-950/80 backdrop-blur supports-[backdrop-filter]:bg-zinc-950/60">
          <div className="max-w-5xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
            <Link href="/" className="group flex items-center gap-2.5">
              <span
                aria-hidden="true"
                className="inline-flex h-7 w-7 items-center justify-center rounded-md
                           bg-gradient-to-br from-cyan-500 via-fuchsia-500 to-purple-600
                           text-xs font-bold text-white shadow-md shadow-purple-900/40
                           group-hover:scale-105 transition-transform"
              >
                ▲
              </span>
              <span className="text-sm font-bold tracking-tight text-zinc-100 group-hover:text-white transition-colors">
                Ship-It
              </span>
            </Link>
            <span className="text-[10px] uppercase tracking-[0.2em] text-zinc-500">
              app factory
            </span>
          </div>
        </header>
        <main className="max-w-5xl mx-auto px-6 py-10">{children}</main>
      </body>
    </html>
  );
}
