// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Compare: overlay the events-per-tick traces of two recordings.

import uPlot from "uplot";
import { api, nums, type Manifest, type RecordingRow } from "../api";
import { h, na } from "../format";
import { axis, lineSeries, PALETTE, xAxis } from "../plot";

interface Pick {
  id: string;
  sid: string;
}

export async function renderCompare(root: HTMLElement, params: URLSearchParams): Promise<() => void> {
  root.replaceChildren(h("p", { class: "loading" }, "Loading…"));
  const rows = (await api.recordings()).filter((r) => r.supported && r.modalities.includes("event"));
  const head = h(
    "div",
    { class: "page-head" },
    h("h1", {}, "Compare"),
    h("p", { class: "sub" }, "Overlay the events-per-tick trace of two recordings, aligned at their first tick."),
  );
  if (rows.length === 0) {
    root.replaceChildren(head, h("div", { class: "card" }, na("No recordings with event sources")));
    return () => {};
  }
  const manifests = new Map<string, Manifest>();
  const eventSources = async (id: string): Promise<string[]> => {
    let m = manifests.get(id);
    if (!m) {
      m = (await api.manifest(id)) ?? undefined;
      if (m) manifests.set(id, m);
    }
    return m ? m.sources.filter((s) => s.modality === "event").map((s) => s.source_id) : [];
  };
  const parse = (v: string | null, fallback: RecordingRow | undefined): Pick => {
    const [id, sid] = (v ?? "").split(":");
    return { id: id || fallback?.id || "", sid: sid ?? "" };
  };
  const picks: [Pick, Pick] = [parse(params.get("a"), rows[0]), parse(params.get("b"), rows[1] ?? rows[0])];

  const chartBox = h("div", { class: "compare-chart" });
  const pickers = h("div", { class: "pickers" });
  root.replaceChildren(head, h("section", { class: "card" }, pickers, chartBox));
  let plot: uPlot | null = null;

  const draw = async () => {
    for (const p of picks) {
      const sids = await eventSources(p.id);
      if (!sids.includes(p.sid)) p.sid = sids[0] ?? "";
    }
    history.replaceState(null, "", `#/compare?a=${picks[0].id}:${picks[0].sid}&b=${picks[1].id}:${picks[1].sid}`);
    pickers.replaceChildren(...(await Promise.all(picks.map((p, i) => picker(p, i)))));
    const traces = await Promise.all(picks.map((p) => (p.sid ? api.trace(p.id, p.sid) : Promise.resolve(null))));
    plot?.destroy();
    plot = null;
    const parts = traces.map((t) => [nums(t, "t_rel_s"), nums(t, "events_per_tick")] as const);
    if (parts.some(([x, y]) => !x || !y)) {
      chartBox.replaceChildren(na("Trace not available for one of the recordings"));
      return;
    }
    const data = uPlot.join(parts.map(([x, y]) => [x as number[], y as number[]]) as uPlot.AlignedData[]);
    chartBox.replaceChildren();
    plot = new uPlot(
      {
        width: Math.max(280, chartBox.clientWidth),
        height: 320,
        legend: { show: true },
        cursor: { drag: { x: true, y: false } },
        scales: { x: { time: false } },
        axes: [xAxis(true), axis("events / tick", 3, 56)],
        series: [
          { label: "t" , value: (_u, v) => (v === null ? "—" : `${v.toFixed(3)} s`) },
          lineSeries(`${picks[0].id} · ${picks[0].sid}`, PALETTE[0] as string),
          lineSeries(`${picks[1].id} · ${picks[1].sid}`, PALETTE[1] as string),
        ],
      },
      data,
      chartBox,
    );
  };

  const picker = async (p: Pick, i: number): Promise<HTMLElement> => {
    const recSel = h("select", { "aria-label": `Recording ${i ? "B" : "A"}` }, ...rows.map((r) => h("option", { value: r.id, selected: r.id === p.id }, r.id)));
    const sids = await eventSources(p.id);
    const srcSel = h("select", { "aria-label": `Source ${i ? "B" : "A"}` }, ...sids.map((s) => h("option", { value: s, selected: s === p.sid }, s)));
    recSel.addEventListener("change", () => {
      p.id = recSel.value;
      p.sid = "";
      void draw();
    });
    srcSel.addEventListener("change", () => {
      p.sid = srcSel.value;
      void draw();
    });
    return h("div", { class: "picker" }, h("span", { class: "swatch", style: `background:${PALETTE[i]}` }), recSel, srcSel);
  };

  const onResize = () => plot?.setSize({ width: Math.max(280, chartBox.clientWidth), height: 320 });
  window.addEventListener("resize", onResize);
  await draw();
  return () => {
    window.removeEventListener("resize", onResize);
    plot?.destroy();
  };
}
