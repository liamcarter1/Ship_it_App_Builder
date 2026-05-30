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
      <body className="min-h-screen font-mono">
        <header className="border-b border-zinc-800 px-6 py-4 flex items-center gap-4">
          <Link href="/" className="font-bold text-zinc-100 hover:text-white">
            Ship-It
          </Link>
          <span className="text-xs text-zinc-500">milestone 2 · read-only dashboard</span>
        </header>
        <main className="max-w-5xl mx-auto px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
