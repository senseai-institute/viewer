// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// What a bench run adds beyond two pipelines: a note when there are no labels, every
// contestant in one table (bench's own numbers from replay_report.json), and the report.

import { api, type ReplayReport, type RunIndex } from "../api/client.ts";
import { armColor } from "../arms.ts";
import { el } from "../components/chrome.ts";
import { render } from "../markdown.ts";
import { labelOf } from "../pages/replay.ts";

/** A visible note about the run's ground truth (null when it is real labels). */
export function gtNote(idx: RunIndex): HTMLElement | null {
  const g = idx.ground_truth;
  if (!g.pseudo || !g.note) return null;
  const box = el("div", "note-banner");
  box.setAttribute("role", "note");
  box.append(el("strong", undefined, "No labels in this recording. "), g.note.replace(/^This recording has no labels\. /, ""));
  return box;
}

function delay(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const ms = Math.round(v * 1000);
  return ms === 0 ? "on time" : ms < 0 ? `${-ms} ms early` : `${ms} ms late`;
}

/**
 * Every contestant: objects caught, reaction, false alarms, latency, and the lead over
 * the contestant on the right of Compare. Rows link to Compare against it.
 */
export function contestants(idx: RunIndex, rep: ReplayReport, right: string): HTMLElement {
  const box = el("section", "panel contest");
  box.append(el("h2", "panel-title", "All contestants"));
  const t = el("table", "grid");
  const rightLabel = labelOf(idx, right);
  t.innerHTML = "";
  const head = el("tr");
  for (const h of ["Contestant", "Objects caught", "Reaction (median)", "False alarms / h", "Latency p99", `Lead over ${rightLabel}`, ""]) head.append(el("th", undefined, h));
  const thead = el("thead");
  thead.append(head);
  const tb = el("tbody");
  const names = Object.keys(rep.arms).sort((a, b) => (rep.arms[a].delay.median_s ?? 99) - (rep.arms[b].delay.median_s ?? 99));
  for (const n of names) {
    const a = rep.arms[n];
    const tr = el("tr");
    const name = el("td");
    const sw = el("span", "swatch");
    sw.style.background = armColor(n);
    name.append(sw, labelOf(idx, n));
    if (n === rep.pseudo_gt_arm) name.append(el("span", "badge warn", "ground truth here"));
    if (a.errors.length) name.title = a.errors.join("; ");
    const lead = n === right ? null : rep.leads[n]?.[right];
    const go = el("td");
    if (n !== right && idx.pipelines.some((p) => p.name === n)) {
      const link = el("a", undefined, "compare");
      link.href = `/compare?a=${encodeURIComponent(`${idx.run_id}:${n}`)}&b=${encodeURIComponent(`${idx.run_id}:${right}`)}`;
      go.append(link);
    }
    tr.append(
      name,
      el("td", "mono", a.errors.length ? "error" : `${a.hits} / ${a.tracks}`),
      el("td", "mono", delay(a.delay.median_s)),
      el("td", "mono", a.unattributed_fires_per_hour === null ? "—" : a.unattributed_fires_per_hour.toFixed(1)),
      el("td", "mono", a.latency.p99_ms === null ? "—" : `${a.latency.p99_ms.toFixed(1)} ms`),
      el("td", "mono", n === right ? "—" : lead && lead.median_s !== null ? `${lead.median_s > 0 ? "+" : ""}${Math.round(lead.median_s * 1000)} ms (n=${lead.n})` : "—"),
      go,
    );
    tb.append(tr);
  }
  t.append(thead, tb);
  const wrap = el("div", "md-table");
  wrap.append(t);
  box.append(wrap);
  const what = rep.mode === "labels" ? "labelled objects" : rep.mode === "agreement" ? `objects ${labelOf(idx, rep.pseudo_gt_arm ?? "yolo")} detected (no labels)` : "objects";
  box.append(el("p", "muted small", `From senseai-bench's report, over ${rep.n_tracks} ${what}. Reaction = how long after an object entered the path each contestant first reacted (early = before it entered). Lead: + means that row reacted earlier.`));
  return box;
}

/** Bench's Markdown report and the world model's training log, folded. */
export function reportPanel(md: string | null, training: string | null): HTMLElement {
  const box = el("section", "panel report");
  if (md) {
    const d = el("details");
    d.append(el("summary", "panel-title", "Report from senseai-bench (replay_report.md)"), render(md));
    box.append(d);
  }
  if (training) {
    const d = el("details");
    const lines = training.trimEnd().split("\n");
    d.append(el("summary", "panel-title", "World model training log"), el("pre", "log", lines.slice(-40).join("\n")));
    box.append(d);
  }
  return box;
}

/** Everything below Compare / Replay for a bench run; empty for runs without a report. */
export async function benchSection(idx: RunIndex, right: string | null): Promise<HTMLElement | null> {
  const has = new Set(idx.attachments ?? []);
  if (!has.size) return null;
  const [rep, md, training] = await Promise.all([
    has.has("report.json") ? api.reportJson(idx.run_id).catch(() => null) : null,
    has.has("report.md") ? api.report(idx.run_id).catch(() => null) : null,
    has.has("training.txt") ? api.training(idx.run_id).catch(() => null) : null,
  ]);
  const sec = el("div", "bench-section");
  if (rep && right) sec.append(contestants(idx, rep, right));
  if (md || training) sec.append(reportPanel(md, training));
  return sec;
}
