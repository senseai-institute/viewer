// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Recordings list: one row per summary (a card on narrow screens).

import { api, type RecordingRow } from "../api";
import { badge, DASH, fmtDate, fmtDuration, fmtInt, fmtMs, h } from "../format";

export async function renderList(root: HTMLElement): Promise<void> {
  root.replaceChildren(h("p", { class: "loading" }, "Loading recordings…"));
  const rows = await api.recordings();
  const head = h(
    "div",
    { class: "page-head" },
    h("h1", {}, "Recordings"),
    h("p", { class: "sub" }, `${rows.length} recording${rows.length === 1 ? "" : "s"}`),
  );
  if (rows.length === 0) {
    root.replaceChildren(
      head,
      h(
        "div",
        { class: "card empty" },
        h("p", {}, "No recordings yet."),
        h("p", { class: "sub" }, "Export one with ", h("code", {}, "senseai-viewer export ticks.h5 --upload"), "."),
      ),
    );
    return;
  }
  const cols = ["Name", "Dataset", "Duration", "Sources", "Jitter p99", "Drops", "Exported"];
  const table = h(
    "table",
    { class: "rec-table" },
    h("thead", {}, h("tr", {}, ...cols.map((c, i) => h("th", { scope: "col", class: i > 1 ? "num" : null }, c)))),
    h("tbody", {}, ...rows.map(row)),
  );
  root.replaceChildren(head, h("div", { class: "card flush" }, table));
}

function row(r: RecordingRow): HTMLElement {
  const href = `#/r/${encodeURIComponent(r.id)}`;
  const jitter =
    r.jitter_p99_ns === null ? badge(DASH, null) : badge(fmtMs(r.jitter_p99_ns), r.jitter_p99_ns < 1e6);
  const drops = r.dropped === null ? badge(DASH, null) : badge(fmtInt(r.dropped), r.dropped === 0);
  const name = h(
    "td",
    { "data-label": "Name", class: "name" },
    h("a", { href, class: "rec-link" }, r.id),
    r.supported ? null : h("div", { class: "warn" }, `summary version ${r.version ?? "?"} not supported: re-export`),
  );
  const tr = h(
    "tr",
    { class: r.supported ? "rec" : "rec unsupported", "data-id": r.id },
    name,
    h("td", { "data-label": "Dataset" }, r.dataset ?? DASH),
    h("td", { "data-label": "Duration", class: "num" }, fmtDuration(r.duration_s)),
    h("td", { "data-label": "Sources", class: "num" }, String(r.n_sources)),
    h("td", { "data-label": "Jitter p99", class: "num" }, jitter),
    h("td", { "data-label": "Drops", class: "num" }, drops),
    h("td", { "data-label": "Exported", class: "num" }, fmtDate(r.created_at)),
  );
  tr.addEventListener("click", (e) => {
    if ((e.target as HTMLElement).closest("a")) return;
    location.hash = href;
  });
  return tr;
}
