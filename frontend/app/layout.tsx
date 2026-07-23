import type { Metadata } from "next";
import { Sidebar } from "@/components/Sidebar";
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
          <div className="flex h-screen overflow-hidden">
            <Sidebar />
            <main className="relative min-w-0 flex-1 overflow-hidden">{children}</main>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
