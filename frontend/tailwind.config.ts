import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Warm paper base rather than flat white — the surface reads as calm at
        // full-screen size, where pure white glares.
        paper: "#faf9f7",
        surface: "#ffffff",
        edge: "#e8e5e0",
        ink: {
          DEFAULT: "#1f1e1d",
          soft: "#5f5b55",
          faint: "#8e8981",
        },
        // Signature colour: prompts in flight. Used for the streaming caret, the
        // in-flight counter, and nothing else, so it always means one thing.
        flight: {
          DEFAULT: "#2f6df6",
          soft: "#eaf1fe",
        },
        pending: "#b08a3e",
        streaming: "#2f6df6",
        complete: "#3f8f5f",
        failed: "#c0453b",
      },
      fontFamily: {
        // No webfont fetch: the hero has to render instantly and offline.
        serif: ["Georgia", "Cambria", "Times New Roman", "serif"],
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "sans-serif",
        ],
      },
      boxShadow: {
        composer: "0 1px 2px rgba(31,30,29,.04), 0 8px 24px rgba(31,30,29,.06)",
        lift: "0 1px 2px rgba(31,30,29,.05), 0 2px 8px rgba(31,30,29,.04)",
      },
    },
  },
  plugins: [],
};

export default config;
