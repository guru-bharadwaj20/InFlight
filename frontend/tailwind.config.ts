import type { Config } from "tailwindcss";

/**
 * Colours are driven by CSS variables (see globals.css) so a single `.dark`
 * class on <html> reskins the whole app. Components reference semantic tokens
 * — surface, ink, edge, flight — never raw hex, so the palette lives in exactly
 * one place and light/dark stay in lockstep.
 */
const withVar = (name: string) => `rgb(var(${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: withVar("--bg"),
        surface: withVar("--surface"),
        "surface-2": withVar("--surface-2"),
        edge: withVar("--edge"),
        ink: {
          DEFAULT: withVar("--ink"),
          soft: withVar("--ink-soft"),
          faint: withVar("--ink-faint"),
        },
        // Brand primary — blue. Reserved for "in flight" and interactive accents.
        flight: {
          DEFAULT: withVar("--flight"),
          soft: withVar("--flight-soft"),
        },
        // Brand secondary — orange. The logo, the hero mark, highlight accents.
        ember: {
          DEFAULT: withVar("--ember"),
          soft: withVar("--ember-soft"),
        },
        // Semantic, each used only for its conventional meaning.
        pending: withVar("--yellow"),
        streaming: withVar("--flight"),
        complete: withVar("--green"),
        failed: withVar("--red"),
        // Kept as aliases so existing markup reads naturally.
        paper: withVar("--bg"),
      },
      fontFamily: {
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
        composer: "0 1px 2px rgb(0 0 0 / 0.06), 0 12px 32px rgb(0 0 0 / 0.10)",
        lift: "0 1px 2px rgb(0 0 0 / 0.06), 0 2px 8px rgb(0 0 0 / 0.05)",
      },
    },
  },
  plugins: [],
};

export default config;
