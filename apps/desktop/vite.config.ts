import fs from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Serve the dev UI over HTTPS with the stack's mkcert certificate (make certs), so the whole
// sign-in round trip stays on HTTPS. WebKit (Safari, Tauri on macOS) stalls when Keycloak
// redirects from https back to an http page.
const certDir = new URL("../../deploy/compose/certs/", import.meta.url);
const https = process.env.FDP_UI_TEST !== "1" && fs.existsSync(new URL("localhost.pem", certDir))
  ? { cert: fs.readFileSync(new URL("localhost.pem", certDir)), key: fs.readFileSync(new URL("localhost-key.pem", certDir)) }
  : undefined;

// Tauri expects a fixed dev port.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 1420, strictPort: true, https },
  build: { target: "es2021", outDir: "dist" },
});
