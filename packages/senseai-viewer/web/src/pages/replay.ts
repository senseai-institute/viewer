// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Replay: one run, scrub and play, toggle profile and layers, read explanations.
// URL: /replay/{run}?t=6.29&p=panther&layers=events,frames,...&cb=1  (and /s/{run}: read-only)

import { api, Chunks, type RunIndex } from "../api/client.ts";
import { legendGradient } from "../colors.ts";
import { copyLink, el, helpDialog, topbar } from "../components/chrome.ts";
import { profileLabel } from "../format.ts";
import { Scene } from "../panels/scene.ts";
import { Explain, Feed, headToHead, Senses } from "../panels/side.ts";
import { Timeline } from "../panels/timeline.ts";
import { bindKeys, Transport } from "../panels/transport.ts";
import { fireInForce, fireNear, sample } from "../sample.ts";
import { LAYERS, type LayerId, parseLayers, Player, UrlState } from "../state/player.ts";
import { benchSection, gtNote } from "../panels/contest.ts";

export const A_COLOR = "#5EEAD4";
export const B_COLOR = "#A9B4CA";

export function startTime(idx: RunIndex): number {
  const tv = idx.ground_truth.t_visible_s;
  return tv !== undefined ? Math.max(0, Math.round((tv - 1.5) * 100) / 100) : 0;
}

export function labelOf(idx: RunIndex, name: string): string {
  const p = idx.pipelines.find((x) => x.name === name);
  if (!p) return name;
  // A bench arm has a plain label ("Panther", "Standard camera AI (torchvision)"); a
  // synthetic or SDK run names pipelines by profile ("Panther v0", "default").
  return p.label && p.label !== p.name ? p.label : profileLabel(p.profile, p.profile_version);
}

export function scaleLegend(colorblind: boolean): HTMLElement {
  const wrap = el("span", "legend");
  wrap.style.margin = "0";
  const bar = el("i");
  bar.style.width = "90px";
  bar.style.height = "8px";
  bar.style.background = legendGradient(colorblind);
  const s = el("span");
  s.append(bar, "surprise low → high");
  wrap.append(s);
  return wrap;
}

export async function replayPage(root: HTMLElement, run: string, share: boolean): Promise<void> {
  const idx = await api.index(run);
  const url = new UrlState();
  const names = idx.pipelines.map((p) => p.name);
  const pair = idx.default_pair;
  let profile = url.get("p") ?? pair?.[0] ?? names[0];
  if (!names.includes(profile)) profile = names[0];
  let layers = parseLayers(url.get("layers"));
  let colorblind = url.get("cb") === "1";
  const player = new Player(idx.duration_s, url.num("t", startTime(idx)));
  const chunks = new Chunks(run, idx.duration_s);
  chunks.onLoad = () => player.invalidate();
  player.canAdvance = (t) => chunks.at(t) !== null;
  document.title = `${idx.title} · SenseAI Viewer`;

  const other = () => (pair ? (profile === pair[0] ? pair[1] : pair[0]) : names.find((n) => n !== profile) ?? null);

  root.replaceChildren(topbar({ run, title: idx.title, synthetic: idx.synthetic, current: "replay", share }));
  const main = el("main", "replay");
  const note = gtNote(idx);
  if (note) root.append(note);
  root.append(main);

  const scenePanel = el("section", "panel scene-wrap");
  const sceneTitle = el("h2", "panel-title", "Scene");
  const legendSlot = el("span");
  sceneTitle.append(el("span", "spacer"), legendSlot);
  const scene = new Scene(idx);
  scenePanel.append(sceneTitle, scene.root);

  const side = el("div", "side");
  const heads = idx.pipelines.find((p) => p.name === profile)?.heads ?? [];
  const detectorLabel = (n: string) => (idx.pipelines.find((p) => p.name === n)?.kind === "detector" ? labelOf(idx, n) : null);
  let senses = new Senses(heads, other() ? labelOf(idx, other()!) : null, detectorLabel(profile));
  const h2h = pair ? headToHead(idx.head_to_head, labelOf(idx, pair[0]), labelOf(idx, pair[1]), [A_COLOR, B_COLOR]) : headToHead(null, "", "");
  const explain = new Explain(idx);
  const feed = new Feed("Events");
  side.append(senses.root, h2h, explain.root, feed.root);

  const series = (pair ?? names.slice(0, 2)).map((n, i) => ({ pipeline: n, label: labelOf(idx, n), color: i ? B_COLOR : A_COLOR }));
  const timeline = new Timeline(idx, series);
  const tlWrap = el("div", "timeline-wrap");
  tlWrap.append(timeline.root);

  const help = helpDialog();
  const setProfile = (n: string) => {
    profile = n;
    const fresh = new Senses(idx.pipelines.find((p) => p.name === n)?.heads ?? [], other() ? labelOf(idx, other()!) : null, detectorLabel(n));
    senses.root.replaceWith(fresh.root);
    senses = fresh;
    feed.set(idx.events.filter((e) => e.pipeline === n), idx.fires.filter((f) => f.pipeline === n));
    url.set({ p: n }, true);
    player.invalidate();
  };
  const toggleLayer = (id: LayerId) => {
    layers = new Set(layers);
    if (layers.has(id)) layers.delete(id);
    else layers.add(id);
    url.set({ layers: [...layers].join(",") }, true);
    player.invalidate();
  };
  const jumpFire = (dir: 1 | -1) => {
    const f = fireNear(idx, profile, player.t, dir);
    if (f) player.seek(f.t_s);
  };
  const shareUrl = () => {
    const q = new URLSearchParams({ t: player.t.toFixed(2), p: profile });
    if (layers.size !== LAYERS.length) q.set("layers", [...layers].join(","));
    if (colorblind) q.set("cb", "1");
    return `${location.origin}/s/${encodeURIComponent(run)}?${q}`;
  };
  const transport = new Transport({
    duration: idx.duration_s,
    profiles: names.length > 1 ? names.map((n) => ({ name: n, label: labelOf(idx, n) })) : null,
    onToggle: () => player.toggle(),
    onSpeed: (s) => player.setSpeed(s),
    onProfile: setProfile,
    onLayer: toggleLayer,
    onFire: jumpFire,
    onColorblind: () => {
      colorblind = !colorblind;
      url.set({ cb: colorblind ? "1" : null }, true);
      player.invalidate();
    },
    onShare: () => {
      void copyLink(shareUrl()).then((ok) => transport.flash(ok ? "Link copied" : shareUrl()));
    },
    onHelp: () => help.showModal(),
  });
  const trWrap = el("div", "transport-wrap");
  trWrap.append(transport.root);
  main.append(scenePanel, side, tlWrap, trWrap);
  void benchSection(idx, pair?.[1] ?? null).then((s) => s && root.append(s));

  timeline.onSeek = (t) => player.seek(t);
  timeline.onFire = (f) => {
    if (f.pipeline !== profile) setProfile(f.pipeline);
    player.seek(f.t_s);
  };
  explain.onSeek = (t) => player.seek(t);
  feed.onSeek = (t) => player.seek(t);
  feed.set(idx.events.filter((e) => e.pipeline === profile), idx.fires.filter((f) => f.pipeline === profile));

  const step = (n: number) => player.seek(player.t + n);
  bindKeys({
    space: () => player.toggle(),
    ArrowLeft: (e) => step(e.shiftKey ? -1 : -idx.tick_ns * 1e-9),
    ArrowRight: (e) => step(e.shiftKey ? 1 : idx.tick_ns * 1e-9),
    "[": () => jumpFire(-1),
    "]": () => jumpFire(1),
    p: () => names.length > 1 && setProfile(names[(names.indexOf(profile) + 1) % names.length]),
    c: () => {
      colorblind = !colorblind;
      player.invalidate();
    },
    e: () => {
      const fs = idx.fires.filter((f) => f.pipeline === profile);
      if (!fs.length) return;
      const near = fs.reduce((a, b) => (Math.abs(b.t_s - player.t) < Math.abs(a.t_s - player.t) ? b : a));
      player.seek(near.t_s);
      explain.root.focus();
    },
    "?": () => help.showModal(),
    ...Object.fromEntries(LAYERS.map((l, i) => [String(i + 1), () => toggleLayer(l.id)])),
  });

  let lastCb: boolean | null = null;
  player.on(() => {
    const t = player.t;
    chunks.prefetch(t);
    const win = chunks.at(t);
    const s = sample(win, profile, t, idx.n_ticks);
    const o = other() ? sample(win, other()!, t, idx.n_ticks) : null;
    const fire = fireInForce(idx, profile, t, s?.action ?? "none");
    scene.render({ t, win, sample: s, layers, colorblind, pathCells: fire?.cells ?? null, profileLabel: labelOf(idx, profile) });
    senses.update(s, o);
    explain.update(fire, profile, t);
    feed.update(t);
    timeline.setT(t);
    transport.update({ t, playing: player.playing, speed: player.speed, profile, layers, colorblind, buffering: win === null });
    if (lastCb !== colorblind) {
      legendSlot.replaceChildren(scaleLegend(colorblind));
      lastCb = colorblind;
    }
    url.set({ t: t.toFixed(2) });
  });
}
