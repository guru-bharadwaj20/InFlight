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
        // Per-job status colours, shared by the bubble states in Stage 4.
        pending: "#a1a1aa",
        streaming: "#38bdf8",
        complete: "#4ade80",
        failed: "#f87171",
      },
    },
  },
  plugins: [],
};

export default config;
