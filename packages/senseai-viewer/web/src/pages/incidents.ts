// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Incidents: every reflex fire and cortex event of a run, filterable; a detail view with the
// ±2 s window around it, the explanation, and a JSON export for a safety report.

import { api, Chunks, type Fire } from "../api/client.ts";
import { el, topbar } from "../components/chrome.ts";
import { Scene } from "../panels/scene.ts";
import { fireInForce, sample } from "../sample.ts";
import { parseLayers, Player } from "../state/player.ts";
import { labelOf } from "./replay.ts";

interface Row {
  type: "fire" | "event";
  pipeline: string;
  t_s: number;
  kind?: string;
  action?: string;
  text?: string;
  explain?: string;
}

const HALF = 2;

export async function incidentsPage(root: HTMLElement, run: string): Promise<void> {
  const idx = await api.index(run);
  const res = await fetch(`/api/runs/${encodeURIComponent(run)}/incidents`);
  const rows = ((await res.json()) as { rows: Row[] }).rows;
  document.title = `Incidents · ${idx.title}`;
  root.replaceChildren(topbar({ run, title: idx.title, synthetic: idx.synthetic, current: "incidents" }));
  const main = el("main");
  root.append(main);
  const hero = el("div", "hero-block");
  hero.append(el("h1", undefined, "Incidents"), el("p", undefined, "Every reflex fire and cortex event in this run. Pick one to see the 2 s either side of it, read the reflex's own explanation, and export the window as JSON for a safety report."));
  main.append(hero);
  const grid = el("div", "incidents");
  const listPanel = el("section", "panel");
  const detail = el("section", "panel");
  grid.append(listPanel, detail);
  main.append(grid);

  const filters = el("div", "filters");
  const selP = el("select");
  selP.setAttribute("aria-label", "Pipeline");
  selP.append(new Option("all pipelines", ""));
  for (const p of idx.pipelines) selP.append(new Option(labelOf(idx, p.name), p.name));
  const selT = el("select");
  selT.setAttribute("aria-label", "Type");
  for (const [v, l] of [["", "fires and events"], ["fire", "reflex fires"], ["event", "cortex events"]]) selT.append(new Option(l, v));
  filters.append(selP, selT);
  const table = el("table", "grid");
  const thead = el("thead");
  thead.innerHTML = "<tr><th>t</th><th>pipeline</th><th>what</th><th>detail</th></tr>";
  const tbody = el("tbody");
  table.append(thead, tbody);
  listPanel.append(el("h2", "panel-title", "Fires and events"), filters, table);

  // Detail: a small scene that loops the ±2 s window.
  const scene = new Scene(idx, { compact: true });
  const dTitle = el("h2", "panel-title", "Detail");
  const sentence = el("p");
  const meta = el("div", "transport");
  const slider = el("input");
  slider.type = "range";
  slider.step = "0.01";
  slider.style.flex = "1";
  slider.setAttribute("aria-label", "Position in the incident window");
  const playBtn = el("button", "btn", "▶ loop");
  const exportA = el("a", "btn", "Export ±2 s JSON");
  const openA = el("a", "btn", "Open in Replay");
  exportA.style.textDecoration = "none";
  openA.style.textDecoration = "none";
  const annot = el("p", "muted", "Annotations arrive with accounts (the database step). The export bundle already includes the window, the fire and the head-to-head numbers.");
  annot.style.fontSize = "12px";
  detail.append(dTitle, sentence, scene.root, meta, annot);
  meta.append(playBtn, slider, exportA, openA);

  const chunks = new Chunks(run, idx.duration_s);
  const player = new Player(idx.duration_s, 0);
  chunks.onLoad = () => player.invalidate();
  let sel: Row | null = null;
  let loop = false;
  const layers = parseLayers(null);

  const select = (r: Row, tr: HTMLTableRowElement) => {
    sel = r;
    tbody.querySelectorAll("tr").forEach((x) => x.setAttribute("aria-selected", String(x === tr)));
    sentence.textContent = r.type === "fire" ? r.explain || r.action || "" : `${r.kind}: ${r.text}`;
    dTitle.textContent = `${r.type === "fire" ? "Reflex fire" : "Cortex event"} · ${labelOf(idx, r.pipeline)} · ${r.t_s.toFixed(2)} s`;
    slider.min = String(Math.max(0, r.t_s - HALF));
    slider.max = String(Math.min(idx.duration_s, r.t_s + HALF));
    slider.value = String(r.t_s);
    exportA.href = api.exportUrl(run, r.t_s, r.pipeline);
    openA.href = `/replay/${encodeURIComponent(run)}?t=${Math.max(0, r.t_s - 1).toFixed(2)}&p=${encodeURIComponent(r.pipeline)}`;
    player.seek(r.t_s);
  };

  const render = () => {
    const fp = selP.value;
    const ft = selT.value;
    tbody.replaceChildren();
    let first: [Row, HTMLTableRowElement] | null = null;
    for (const r of rows) {
      if ((fp && r.pipeline !== fp) || (ft && r.type !== ft)) continue;
      const tr = el("tr", "row");
      tr.tabIndex = 0;
      const what = r.type === "fire" ? el("span", "badge warn", r.action ?? "fire") : el("span", "mono", r.kind ?? "");
      const tds = [el("td", "mono", r.t_s.toFixed(2)), el("td", undefined, labelOf(idx, r.pipeline)), el("td"), el("td", undefined, r.type === "fire" ? r.explain ?? "" : r.text ?? "")];
      tds[2].append(what);
      tr.append(...tds);
      tr.addEventListener("click", () => select(r, tr));
      tr.addEventListener("keydown", (e) => e.key === "Enter" && select(r, tr));
      tbody.append(tr);
      if (!first) first = [r, tr];
    }
    if (!tbody.children.length) tbody.innerHTML = "<tr><td colspan=4 class=muted>Nothing matches.</td></tr>";
    if (first && (!sel || !rows.includes(sel))) select(...first);
    else if (first && sel) {
      const i = [...tbody.children].findIndex((x) => x === tbody.children[0]);
      if (i < 0) select(...first);
    }
  };
  selP.addEventListener("change", () => {
    sel = null;
    render();
  });
  selT.addEventListener("change", () => {
    sel = null;
    render();
  });
  slider.addEventListener("input", () => {
    loop = false;
    if (player.playing) player.toggle();
    player.seek(Number(slider.value));
  });
  playBtn.addEventListener("click", () => {
    loop = !player.playing;
    if (sel && player.t >= Number(slider.max) - 0.02) player.seek(Number(slider.min));
    player.toggle();
  });
  player.canAdvance = (t) => chunks.at(t) !== null;
  player.on(() => {
    if (!sel) return;
    let t = player.t;
    if (player.playing && t >= Number(slider.max)) {
      if (loop) player.seek(Number(slider.min));
      else player.toggle();
      t = player.t;
    }
    playBtn.textContent = player.playing ? "❚❚ pause" : "▶ loop";
    slider.value = String(t);
    chunks.prefetch(t);
    const win = chunks.at(t);
    const s = sample(win, sel.pipeline, t, idx.n_ticks);
    const f: Fire | null = fireInForce(idx, sel.pipeline, t, s?.action ?? "none");
    scene.render({ t, win, sample: s, layers, colorblind: false, pathCells: f?.cells ?? null, profileLabel: labelOf(idx, sel.pipeline) });
  });
  render();
}
