// Canonical palette for charts + inline SVG — the one place chart colors live.
// Keep in sync with the CSS variables in index.css (canvas-rendered charts can't
// read CSS custom properties, so these are concrete values).
export const C = {
  accent: "#4da3ff",
  accent2: "#7dd3fc",
  green: "#2ee08a",
  red: "#ff5c72",
  amber: "#f0b43e",
  violet: "#a78bfa",
  fg2: "#9db0c7",
  muted: "#5f7189",
  grid: "rgba(148, 180, 220, 0.07)",
  border: "rgba(148, 180, 220, 0.16)",
  accentSoft: "rgba(77, 163, 255, 0.25)",
  areaTop: "rgba(77, 163, 255, 0.22)",
  areaBottom: "rgba(77, 163, 255, 0.0)",
  mono: '"JetBrains Mono", ui-monospace, Consolas, monospace',
} as const;
