import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During dev (npm run dev) the Vite server proxies /api to the FastAPI backend.
// In production the FastAPI server serves the built dist/ directly.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
