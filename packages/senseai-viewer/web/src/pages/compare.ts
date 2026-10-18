// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Compare: two pipelines (of one run, or of two runs) side by side on one clock, with the
// head-to-head tiles in the middle. URL: /compare/{run} or /compare?a=run:pipe&b=run:pipe

import { api, Chunks, type RunIndex } from "../api/client.ts";
import { copyLink, el, helpDialog, topbar } from "../components/chrome.ts";
import { Scene } from "../panels/scene.ts";
import { benchSection, gtNote } from "../panels/contest.ts";
import { Explain, headToHead } from "../panels/side.ts";
import { Timeline } from "../panels/timeline.ts";
import { bindKeys, Transport } from "../panels/transport.ts";
import { fireInForce, sample } from "../sample.ts";
import { LAYERS, type LayerId, parseLayers, Player, UrlState } from "../state/player.ts";
import { A_COLOR, B_COLOR, labelOf, scaleLegend, startTime } from "./replay.ts";

interface SideState {
  run: string;
  pipeline: string;
  idx: RunIndex;
  chunks: Chunks;
  scene: Scene;
  explain: Explain;
  color: string;
}

export async function comparePage(root: HTMLElement, pathRun: string | null): Promise<void> {
  const url = new UrlState();
  let a = url.get("a");
  let b = url.get("b");
  if (!a || !b) {
    const run = pathRun ?? (await api.runs())[0]?.run_id;
    if (!run) throw new Error("no runs to compare");
    const idx = await api.index(run);
    const pair = idx.default_pair ?? [idx.pipelines[0].name, idx.pipelines[0].name];
    a = `${run}:${pair[0]}`;
    b = `${run}:${pair[1]}`;
  }
  const cmp = await api.compare(a, b);
  const offset = cmp.clock.offset_s;
  const idxA = await api.index(cmp.a.run_id);
  const idxB = cmp.same_run ? idxA : await api.index(cmp.b.run_id);
  const chunksA = new Chunks(cmp.a.run_id, idxA.duration_s);
  const chunksB = cmp.same_run ? chunksA : new Chunks(cmp.b.run_id, idxB.duration_s);
  const player = new Player(idxA.duration_s, url.num("t", startTime(idxA)));
  chunksA.onLoad = () => player.invalidate();
  chunksB.onLoad = () => player.invalidate();
  player.canAdvance = (t) => chunksA.at(t) !== null && chunksB.at(t + offset) !== null;
  let layers = parseLayers(url.get("layers"));
  let colorblind = url.get("cb") === "1";
  let diff = url.get("diff") === "1";
  const labA = labelOf(idxA, cmp.a.pipeline);
  const labB = labelOf(idxB, cmp.b.pipeline);
  document.title = `${labA} vs ${labB} · SenseAI Viewer`;

  root.replaceChildren(topbar({ run: cmp.a.run_id, title: `${labA} vs ${labB} — ${idxA.title}`, synthetic: idxA.synthetic || idxB.synthetic, current: "compare" }));
  const main = el("main", "compare");
  const gt = gtNote(idxA);
  if (gt) root.append(gt);
  root.append(main);

  const mkSide = (run: string, pipeline: string, idx: RunIndex, chunks: Chunks, color: string, cls: string): [SideState, HTMLElement] => {
    const wrap = el("section", `panel ${cls}`);
    const head = el("div", "side-head");
    const dot = el("span", "dot");
    dot.style.background = color;
    head.append(dot, labelOf(idx, pipeline), el("span", "muted", cmp.same_run ? "" : ` · ${idx.title}`));
    const scene = new Scene(idx, { stacked: true });
    const explain = new Explain(idx);
    wrap.append(head, scene.root);
    const extra = el("div");
    extra.style.display = "grid";
    extra.style.gap = "10px";
    extra.style.marginTop = "10px";
    extra.append(explain.root);
    wrap.append(extra);
    return [{ run, pipeline, idx, chunks, scene, explain, color }, wrap];
  };
  const [sa, wa] = mkSide(cmp.a.run_id, cmp.a.pipeline, idxA, chunksA, A_COLOR, "side-a");
  const [sb, wb] = mkSide(cmp.b.run_id, cmp.b.pipeline, idxB, chunksB, B_COLOR, "side-b");

  const mid = el("div", "mid");
  const tiles = headToHead(cmp.head_to_head, labA, labB, [A_COLOR, B_COLOR]);
  const controls = el("section", "panel");
  controls.append(el("h2", "panel-title", "Compare"));
  const row = el("div", "transport");
  const swap = el("button", "btn", "Swap sides");
  swap.addEventListener("click", () => {
    const q = new URLSearchParams(location.search);
    q.set("a", b!);
    q.set("b", a!);
    q.set("t", player.t.toFixed(2));
    location.search = q.toString();
  });
  const diffBtn = el("button", "btn", "Diff");
  diffBtn.title = "Outline the cells lit on one side but not the other";
  diffBtn.addEventListener("click", () => {
    diff = !diff;
    url.set({ diff: diff ? "1" : null }, true);
    player.invalidate();
  });
  row.append(swap, diffBtn);
  const note = el("p", "muted");
  note.style.fontSize = "12px";
  note.style.margin = "10px 0 0";
  note.textContent = cmp.same_run
    ? "Both pipelines ran on the same recording, so the clocks are identical."
    : `Clocks aligned on the ground-truth event (B shifted by ${offset.toFixed(2)} s).`;
  const legendRow = el("div");
  legendRow.style.marginTop = "10px";
  controls.append(row, note, legendRow);
  mid.append(tiles, controls);

  const timeline = new Timeline(idxA, [
    { pipeline: cmp.a.pipeline, label: labA, color: A_COLOR, index: idxA },
    { pipeline: cmp.b.pipeline, label: labB, color: B_COLOR, index: idxB, offset_s: offset },
  ]);
  const tlWrap = el("div", "timeline-wrap");
  tlWrap.append(timeline.root);
  const help = helpDialog();
  const toggleLayer = (id: LayerId) => {
    layers = new Set(layers);
    if (layers.has(id)) layers.delete(id);
    else layers.add(id);
    url.set({ layers: [...layers].join(",") }, true);
    player.invalidate();
  };
  const fires = (dir: 1 | -1) => {
    const ts = [...idxA.fires.filter((f) => f.pipeline === cmp.a.pipeline).map((f) => f.t_s), ...idxB.fires.filter((f) => f.pipeline === cmp.b.pipeline).map((f) => f.t_s - offset)].sort((x, y) => x - y);
    const t = player.t;
    const next = dir > 0 ? ts.find((x) => x > t + 0.005) : [...ts].reverse().find((x) => x < t - 0.005);
    if (next !== undefined) player.seek(next);
  };
  const transport = new Transport({
    duration: idxA.duration_s,
    profiles: null,
    onToggle: () => player.toggle(),
    onSpeed: (s) => player.setSpeed(s),
    onProfile: () => {},
    onLayer: toggleLayer,
    onFire: fires,
    onColorblind: () => {
      colorblind = !colorblind;
      player.invalidate();
    },
    onShare: () => {
      void copyLink(location.href).then((ok) => transport.flash(ok ? "Link copied" : location.href));
    },
    onHelp: () => help.showModal(),
  });
  const trWrap = el("div", "transport-wrap");
  trWrap.append(transport.root);
  main.append(wa, mid, wb, trWrap, tlWrap);
  if (cmp.same_run) void benchSection(idxA, cmp.b.pipeline).then((s) => s && root.append(s));

  timeline.onSeek = (t) => player.seek(t);
  timeline.onFire = (f) => player.seek(f.pipeline === cmp.b.pipeline && !cmp.same_run ? f.t_s - offset : f.t_s);
  sa.explain.onSeek = (t) => player.seek(t);
  sb.explain.onSeek = (t) => player.seek(t - offset);
  bindKeys({
    space: () => player.toggle(),
    ArrowLeft: (e) => player.seek(player.t - (e.shiftKey ? 1 : idxA.tick_ns * 1e-9)),
    ArrowRight: (e) => player.seek(player.t + (e.shiftKey ? 1 : idxA.tick_ns * 1e-9)),
    "[": () => fires(-1),
    "]": () => fires(1),
    c: () => {
      colorblind = !colorblind;
      player.invalidate();
    },
    d: () => diffBtn.click(),
    "?": () => help.showModal(),
    ...Object.fromEntries(LAYERS.map((l, i) => [String(i + 1), () => toggleLayer(l.id)])),
  });

  let lastCb: boolean | null = null;
  player.on(() => {
    const t = player.t;
    chunksA.prefetch(t);
    chunksB.prefetch(t + offset);
    const wA = chunksA.at(t);
    const wB = chunksB.at(t + offset);
    const sA = sample(wA, sa.pipeline, t, idxA.n_ticks);
    const sB = sample(wB, sb.pipeline, t + offset, idxB.n_ticks);
    const fA = fireInForce(idxA, sa.pipeline, t, sA?.action ?? "none");
    const fB = fireInForce(idxB, sb.pipeline, t + offset, sB?.action ?? "none");
    sa.scene.render({ t, win: wA, sample: sA, layers, colorblind, pathCells: fA?.cells ?? null, diff: diff ? sB?.grid ?? null : null, profileLabel: labA });
    sb.scene.render({ t: t + offset, win: wB, sample: sB, layers, colorblind, pathCells: fB?.cells ?? null, diff: diff ? sA?.grid ?? null : null, profileLabel: labB });
    sa.explain.update(fA, sa.pipeline, t);
    sb.explain.update(fB, sb.pipeline, t + offset);
    timeline.setT(t);
    diffBtn.setAttribute("aria-pressed", String(diff));
    transport.update({ t, playing: player.playing, speed: player.speed, profile: null, layers, colorblind, buffering: !wA || !wB });
    if (lastCb !== colorblind) {
      legendRow.replaceChildren(scaleLegend(colorblind));
      lastCb = colorblind;
    }
    url.set({ t: t.toFixed(2) });
  });
}
