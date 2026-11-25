import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, forward API calls to a locally running Pi agent.
const agent = process.env.AGENT_URL ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": agent, "/healthz": agent } },
});
