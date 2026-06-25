import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Прокси /api на FastAPI (dev). В проде SPA отдаётся самим FastAPI из web/dist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    proxy: {
      "/api": "http://127.0.0.1:8077",
    },
  },
});
