import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Per-source accent colours, mirroring the CLI printer.
        src: {
          orchestrator: '#22d3ee', // cyan
          planner: '#e879f9',      // magenta
          scaffolder: '#60a5fa',   // blue
          coder: '#4ade80',        // green
          reviewer: '#f87171',     // red
          deployer: '#c084fc',     // bright purple
          tool: '#fbbf24',         // yellow
          system: '#9ca3af',       // grey
        },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'monospace'],
      },
    },
  },
  plugins: [],
};

export default config;
