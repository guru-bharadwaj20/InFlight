import type { Metadata } from "next";
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
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Sets the theme class before first paint so a dark reload never flashes light. */}
        <script dangerouslySetInnerHTML={{ __html: noFlashScript }} />
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
