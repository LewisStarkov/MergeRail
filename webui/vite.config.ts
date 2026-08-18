import preact from "@preact/preset-vite";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  plugins: [preact()],
  build: {
    outDir: "../src/mergerail/fronts/web_assets/dist",
    emptyOutDir: true,
    manifest: true,
  },
  test: { environment: "node" },
});
