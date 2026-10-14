// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
import { defineConfig } from "vite";

// The build lands inside the Python package so FastAPI serves it from one image.
export default defineConfig({
  base: "/",
  build: {
    outDir: "../src/senseai_viewer/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 300,
  },
  server: {
    proxy: { "/api": "http://localhost:8000", "/healthz": "http://localhost:8000" },
  },
});
