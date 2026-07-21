import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Concurrent LLM Chat",
  description:
    "Non-blocking chat where several prompts generate at once against one shared history.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="mx-auto flex min-h-screen max-w-4xl flex-col px-6 py-10">
          <header className="mb-8 border-b border-zinc-800 pb-6">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              Concurrent LLM Chat
            </Link>
            <p className="mt-1 text-sm text-zinc-400">
              Stage 1 — foundations: schema, services, and the snapshot rule.
            </p>
          </header>
          <main className="flex-1">{children}</main>
        </div>
      </body>
    </html>
  );
}
