// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Router. Plain links and full page loads; every page keeps its state in the URL.
//   /                      runs
//   /replay/{run}          replay          /s/{run}  read-only share of the same page
//   /compare/{run}         compare the run's default pair; /compare?a=run:pipe&b=run:pipe
//   /incidents/{run}       incidents
// "demo" as {run} means the first synthetic run on the server.

import "./theme.css";
import { api } from "./api/client.ts";
import { el } from "./components/chrome.ts";
import { comparePage } from "./pages/compare.ts";
import { incidentsPage } from "./pages/incidents.ts";
import { replayPage } from "./pages/replay.ts";
import { runsPage } from "./pages/runs.ts";

async function resolve(run: string): Promise<string> {
  if (run !== "demo") return run;
  const runs = await api.runs();
  const r = runs.find((x) => x.synthetic) ?? runs[0];
  if (!r) throw new Error("no runs on this server yet");
  return r.run_id;
}

async function route(root: HTMLElement): Promise<void> {
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  const [page, arg] = parts;
  if (!page) return runsPage(root);
  if ((page === "replay" || page === "s") && arg) return replayPage(root, await resolve(arg), page === "s");
  if (page === "compare") return comparePage(root, arg ? await resolve(arg) : null);
  if (page === "incidents" && arg) return incidentsPage(root, await resolve(arg));
  root.replaceChildren(el("main", "loading", "Page not found. "));
  const a = el("a", undefined, "All runs");
  a.href = "/";
  root.firstElementChild!.append(a);
}

const root = document.getElementById("app")!;
root.append(el("div", "loading", "loading…"));
route(root).catch((e: unknown) => {
  root.replaceChildren(el("main", "loading error", `Could not load: ${e instanceof Error ? e.message : String(e)}`));
});
