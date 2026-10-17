// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Runs: every recorded run on this server, with links to Replay, Compare and Incidents.

import { api } from "../api/client.ts";
import { el, topbar } from "../components/chrome.ts";

export async function runsPage(root: HTMLElement): Promise<void> {
  const runs = await api.runs();
  document.title = "SenseAI Viewer";
  root.replaceChildren(topbar({ current: "runs" }));
  const main = el("main");
  root.append(main);
  const hero = el("div", "hero-block");
  hero.append(
    el("h1", undefined, "What the robot sensed, predicted and did"),
    el("p", undefined, "Open a run to replay it: where something unexpected appeared, which sense noticed it, when the reflex slowed the robot, and why. Compare two profiles on the same recording to see the difference in milliseconds."),
  );
  main.append(hero);
  const cards = el("div", "cards");
  for (const r of runs) {
    const c = el("article", "panel card");
    const h = el("h3", undefined, r.title);
    const facts = el("div", "facts");
    facts.append(el("span", undefined, `${r.duration_s.toFixed(1)} s`), el("span", undefined, `${r.pipelines.length} pipeline${r.pipelines.length === 1 ? "" : "s"}: ${r.pipelines.join(", ")}`), el("span", undefined, `${r.fires} fire${r.fires === 1 ? "" : "s"}`));
    if (r.scenario) facts.append(el("span", "mono", r.scenario));
    const actions = el("div", "actions");
    const id = encodeURIComponent(r.run_id);
    const link = (label: string, href: string, primary = false) => {
      const a = el("a", `btn${primary ? " primary" : ""}`, label);
      a.href = href;
      actions.append(a);
    };
    link("Replay", `/replay/${id}`, true);
    if (r.pipelines.length > 1) link("Compare", `/compare/${id}`);
    link("Incidents", `/incidents/${id}`);
    link("Share link", `/s/${id}`);
    c.append(h);
    if (r.synthetic) c.append(el("span", "badge warn", "synthetic data"));
    c.append(facts, actions);
    cards.append(c);
  }
  if (!runs.length) cards.append(el("p", "muted", "No runs yet. Upload a RunLog with PUT /api/runs (see the README)."));
  main.append(cards);
}
