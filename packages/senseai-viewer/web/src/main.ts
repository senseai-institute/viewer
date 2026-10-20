// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Router. Plain links and full page loads; every page keeps its state in the URL.
//   /                      the Run page when the runner is on, else the simple view of the demo run
//   /s/{run}, /story/{run} the simple view: side-by-side video for runs with footage, else the story
//   /run                   Setup, Data, Run, Progress, Results: one click from S3 to a report (SENSEAI_RUNNER=1)
//   /runs                  all runs
//   /replay/{run}          the detailed view
//   /compare/{run}         compare the run's default pair; /compare?a=run:pipe&b=run:pipe
//   /incidents/{run}       incidents
//   /research              the research assistant and evidence ledger (when RESEARCH_URL is set)
// "demo" as {run} means the first synthetic run on the server.

import "./theme.css";
import { api, run } from "./api/client.ts";
import { el, features } from "./components/chrome.ts";
import { comparePage } from "./pages/compare.ts";
import { incidentsPage } from "./pages/incidents.ts";
import { replayPage } from "./pages/replay.ts";
import { researchPage } from "./pages/research.ts";
import { runsPage } from "./pages/runs.ts";
import { runPage } from "./pages/run.ts";
import { storyPage } from "./pages/story.ts";
import { simpleView } from "./pages/video.ts";

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
  features.runner = await run.status().then((o) => o.enabled, () => false);
  features.research = await fetch("/api/research-status").then((r) => (r.ok ? r.json() : { enabled: false })).then((o) => !!o.enabled, () => false);
  if (!page) return features.runner ? runPage(root) : simpleView(root, await resolve("demo"), storyPage);
  if (page === "run") return runPage(root);
  if (page === "research") return researchPage(root);
  if ((page === "s" || page === "story") && arg) return simpleView(root, await resolve(arg), storyPage);
  if (page === "runs") return runsPage(root);
  if (page === "replay" && arg) return replayPage(root, await resolve(arg), false);
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
