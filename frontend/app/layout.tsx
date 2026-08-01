import type { Metadata } from "next";
import { headers } from "next/headers";
import { AppShell } from "@/components/AppShell";
import { AuthProvider } from "@/lib/useAuth";
import { ThemeProvider, noFlashScript } from "@/components/theme";
import "./globals.css";

export const metadata: Metadata = {
  title: "InFlight",
  description:
    "A chat where the input never locks. Several prompts generate at once against one shared history.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // Minted per request by middleware.ts. The no-flash script has to run inline
  // in <head> before hydration, which the CSP would otherwise block.
  const nonce = headers().get("x-nonce") ?? undefined;

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Sets the theme class before first paint so a dark reload never flashes light. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: noFlashScript }} />
      </head>
      <body>
        <ThemeProvider>
          <AuthProvider>
            <AppShell>{children}</AppShell>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
