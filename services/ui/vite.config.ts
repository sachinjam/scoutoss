import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 2000 },
  server: { port: 5173 },
  test: { environment: "jsdom", globals: true },
});
