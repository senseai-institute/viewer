// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Video view (the simple view for runs with footage): two synced players on the recorded
// video, Panther on the left and a baseline on the right, with switchable overlays, a big
// status under each, and a shared timeline of who reacted first to each labelled object.
// URL: /s/{run}?a=<arm>&b=<arm>&t=<s>

import { api, type Box, type OverlayArm, type Overlays, type Reactions, type RunIndex, videoApi, type VideoMeta } from "../api/client.ts";
import { armColor, statusOf } from "../arms.ts";
import { el, topbar } from "../components/chrome.ts";
import { Player, SPEEDS, UrlState } from "../state/player.ts";
import { gtNote } from "../panels/contest.ts";

const CHUNK_S = 10;
const AHEAD_S = 2.5;
const MAX_INFLIGHT = 6;
const KEEP_FRAMES = 400;

type Overlay = "gt" | "boxes" | "heat" | "events";
const OVERLAYS: { id: Overlay; label: string; key: string }[] = [
  { id: "gt", label: "Ground truth", key: "1" },
  { id: "boxes", label: "Detector boxes", key: "2" },
  { id: "heat", label: "Panther heat", key: "3" },
  { id: "events", label: "Event camera", key: "4" },
];

/** Loads JPEG frames ahead of the playhead, a few at a time, and keeps a window of them. */
class Frames {
  private imgs = new Map<string, HTMLImageElement>();
  private loading = new Set<string>();
  private queue: [number, "rgb" | "event"][] = [];
  onLoad: () => void = () => {};
  constructor(private run: string, private n: number) {}

  private key(i: number, kind: string): string {
    return `${kind}:${i}`;
  }

  get(i: number, kind: "rgb" | "event" = "rgb"): HTMLImageElement | null {
    return this.imgs.get(this.key(i, kind)) ?? null;
  }

  want(from: number, to: number, kind: "rgb" | "event"): void {
    this.queue = [];
    for (let i = Math.max(0, from); i <= Math.min(this.n - 1, to); i++) {
      const k = this.key(i, kind);
      if (!this.imgs.has(k) && !this.loading.has(k)) this.queue.push([i, kind]);
    }
    this.pump();
    if (this.imgs.size > KEEP_FRAMES) {
      for (const k of [...this.imgs.keys()]) {
        const i = Number(k.split(":")[1]);
        if (i < from - 60 || i > to + 60) this.imgs.delete(k);
      }
    }
  }

  private pump(): void {
    while (this.loading.size < MAX_INFLIGHT && this.queue.length) {
      const [i, kind] = this.queue.shift()!;
      const k = this.key(i, kind);
      this.loading.add(k);
      const img = new Image();
      img.decoding = "async";
      img.src = videoApi.frameUrl(this.run, i, kind);
      img
        .decode()
        .then(
          () => this.imgs.set(k, img),
          () => undefined,
        )
        .finally(() => {
          this.loading.delete(k);
          this.onLoad();
          this.pump();
        });
    }
  }
}

/** Overlay data in 10 s chunks. */
class OverlayChunks {
  private done = new Map<number, Overlays>();
  private pending = new Set<number>();
  onLoad: () => void = () => {};
  constructor(private run: string, private duration: number) {}

  at(t: number): Overlays | null {
    const k = Math.floor(Math.max(0, t) / CHUNK_S);
    this.load(k);
    if (t % CHUNK_S > CHUNK_S - AHEAD_S) this.load(k + 1);
    return this.done.get(k) ?? null;
  }

  private load(k: number): void {
    if (k * CHUNK_S >= this.duration || this.done.has(k) || this.pending.has(k)) return;
    this.pending.add(k);
    videoApi
      .overlays(this.run, k * CHUNK_S, Math.min(this.duration + 0.001, (k + 1) * CHUNK_S))
      .then((o) => this.done.set(k, o))
      .catch(() => undefined)
      .finally(() => {
        this.pending.delete(k);
        this.onLoad();
      });
  }
}

/** One player: the frame, the overlays for one contestant, and its status pill. */
class Pane {
  readonly root: HTMLElement;
  readonly canvas: HTMLCanvasElement;
  readonly title: HTMLElement;
  readonly select: HTMLSelectElement;
  readonly pill: HTMLElement;
  private ctx: CanvasRenderingContext2D;
  arm: string;

  constructor(idx: RunIndex, choices: string[], arm: string, side: "a" | "b", onPick: (arm: string) => void) {
    this.arm = arm;
    this.root = el("div", `vpane ${side}`);
    const head = el("div", "vpane-head");
    this.title = el("span", "vpane-title");
    this.select = el("select");
    this.select.setAttribute("aria-label", side === "a" ? "Left player contestant" : "Right player contestant");
    for (const c of choices) this.select.append(new Option(idx.pipelines.find((p) => p.name === c)?.label ?? c, c));
    this.select.value = arm;
    this.select.addEventListener("change", () => onPick(this.select.value));
    head.append(this.title, this.select);
    this.canvas = el("canvas");
    this.canvas.width = idx.video?.width || 640;
    this.canvas.height = idx.video?.height || 480;
    this.canvas.setAttribute("role", "img");
    this.pill = el("div", "story-status ok", "Driving normally");
    this.pill.setAttribute("aria-live", "polite");
    this.root.append(head, this.canvas, this.pill);
    this.ctx = this.canvas.getContext("2d")!;
    this.setArm(idx, arm);
  }

  setArm(idx: RunIndex, arm: string): void {
    this.arm = arm;
    this.select.value = arm;
    const label = idx.pipelines.find((p) => p.name === arm)?.label ?? arm;
    this.title.textContent = label;
    this.title.style.color = armColor(arm);
    this.root.style.borderColor = armColor(arm);
    this.canvas.setAttribute("aria-label", `Recorded video with ${label}'s view drawn on top`);
  }

  draw(o: {
    img: HTMLImageElement | null;
    ev: HTMLImageElement | null;
    meta: VideoMeta;
    frame: number;
    ov: Overlays | null;
    show: Set<Overlay>;
    bearingScale: number;
  }): void {
    const ctx = this.ctx;
    const W = this.canvas.width;
    const H = this.canvas.height;
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    if (o.img) ctx.drawImage(o.img, 0, 0, W, H);
    else {
      ctx.fillStyle = "#04060b";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#9AA4B8";
      ctx.font = "18px 'IBM Plex Sans', sans-serif";
      ctx.fillText("loading video…", 20, 34);
    }
    if (o.show.has("events") && o.ev) {
      ctx.globalCompositeOperation = "screen";
      ctx.globalAlpha = 0.6;
      ctx.drawImage(o.ev, 0, 0, W, H);
      ctx.globalCompositeOperation = "source-over";
      ctx.globalAlpha = 1;
    }
    const arm: OverlayArm | undefined = o.ov?.arms[this.arm];
    const j = o.ov ? o.frame - o.ov.frame0 : -1;
    // Panther heat band: the bearing ring, bin by bin, projected onto image columns.
    if (o.show.has("heat") && arm?.bearing && j >= 0 && j < arm.bearing.length && o.meta.fx && o.meta.cx !== null) {
      const ring = arm.bearing[j];
      const B = ring.length;
      const half = Math.PI / B;
      for (let b = 0; b < B; b++) {
        const n = Math.min(1, ring[b] / Math.max(1e-6, o.bearingScale));
        if (n < 0.35) continue;
        let th = (b * 2 * Math.PI) / B;
        if (th > Math.PI) th -= 2 * Math.PI; // bins are CCW from straight ahead; left is positive
        const lo = th - half;
        const hi = th + half;
        if (Math.abs(lo) > 1.45 || Math.abs(hi) > 1.45) continue;
        const u1 = o.meta.cx - o.meta.fx * Math.tan(hi);
        const u2 = o.meta.cx - o.meta.fx * Math.tan(lo);
        const a = 0.12 + 0.4 * (n - 0.35) / 0.65;
        const g = ctx.createLinearGradient(0, 0, 0, H);
        g.addColorStop(0, `rgba(255,90,122,0)`);
        g.addColorStop(0.45, `rgba(255,${Math.round(180 - 90 * n)},${Math.round(122 - 60 * n)},${a.toFixed(3)})`);
        g.addColorStop(1, `rgba(255,90,122,${(a * 0.5).toFixed(3)})`);
        ctx.fillStyle = g;
        ctx.fillRect(Math.min(u1, u2), 0, Math.abs(u2 - u1) + 1, H);
      }
    }
    if (o.show.has("boxes") && arm?.boxes) {
      if (arm.corridor) {
        ctx.strokeStyle = "rgba(245,158,11,0.35)";
        ctx.setLineDash([6, 6]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        arm.corridor.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.stroke();
        ctx.setLineDash([]);
      }
      for (const bx of arm.boxes[String(o.frame)] ?? []) this.box(bx, armColor(this.arm), bx[6] === true, `${bx[4]} ${typeof bx[5] === "number" ? bx[5].toFixed(2) : ""}`);
    }
    if (o.show.has("gt") && o.ov) {
      for (const bx of o.ov.gt[String(o.frame)] ?? []) this.box(bx, "#ffffff", false, `${bx[4]}${bx[5] != null ? ` #${bx[5]}` : ""}`, true);
    }
    const st = statusOf(arm?.action?.[j]);
    if (this.pill.textContent !== st.text) {
      this.pill.textContent = st.text;
      this.pill.className = `story-status ${st.cls}`;
    }
  }

  private box(b: Box, color: string, solid: boolean, label: string, thin = false): void {
    const ctx = this.ctx;
    ctx.strokeStyle = color;
    ctx.lineWidth = thin ? 1.25 : solid ? 3 : 1.5;
    if (!solid && !thin) ctx.setLineDash([5, 4]);
    ctx.strokeRect(b[0], b[1], b[2], b[3]);
    ctx.setLineDash([]);
    ctx.font = "500 13px 'IBM Plex Mono', monospace";
    const w = ctx.measureText(label).width + 8;
    const y = thin ? b[1] + b[3] + 16 : b[1] - 6;
    ctx.fillStyle = "rgba(12,17,27,0.75)";
    ctx.fillRect(b[0] - 1, y - 13, w, 17);
    ctx.fillStyle = color;
    ctx.fillText(label, b[0] + 3, y);
  }
}

export async function videoPage(root: HTMLElement, run: string, idx: RunIndex): Promise<void> {
  const url = new UrlState();
  const meta = await videoApi.meta(run);
  const choices = idx.pipelines.filter((p) => p.layers.reflex).map((p) => p.name);
  const pair = idx.video_pair ?? [choices[0], choices[1] ?? choices[0]];
  let a = url.get("a") ?? pair[0];
  let b = url.get("b") ?? pair[1];
  if (!choices.includes(a)) a = pair[0];
  if (!choices.includes(b)) b = pair[1];
  const show = new Set<Overlay>(["gt", "boxes", "heat"]);
  const duration = meta.t_s.length ? meta.t_s[meta.t_s.length - 1] : idx.duration_s;
  const player = new Player(duration, url.num("t", 0));
  const frames = new Frames(run, meta.frames);
  const ovs = new OverlayChunks(run, duration);
  frames.onLoad = () => player.invalidate();
  ovs.onLoad = () => player.invalidate();
  let rx: Reactions = await videoApi.reactions(run, a, b);
  document.title = `${idx.title} · SenseAI`;

  root.replaceChildren(topbar({ run, title: idx.title, synthetic: idx.synthetic, current: "story" }));
  const main = el("main", "video-page");
  root.append(main);

  const head = el("header", "story-head");
  const h1 = el("h1");
  const sub = el("p");
  head.append(h1, sub);
  main.append(head);
  const gt = gtNote(idx);
  if (gt) main.append(gt);

  const views = el("section", "vpanes");
  const repick = async (side: "a" | "b", arm: string) => {
    if (side === "a") a = arm;
    else b = arm;
    paneA.setArm(idx, a);
    paneB.setArm(idx, b);
    url.set({ a, b }, true);
    rx = await videoApi.reactions(run, a, b);
    headline();
    renderObjects();
    player.invalidate();
  };
  const paneA = new Pane(idx, choices, a, "a", (x) => void repick("a", x));
  const paneB = new Pane(idx, choices, b, "b", (x) => void repick("b", x));
  views.append(paneA.root, paneB.root);
  main.append(views);

  // Controls.
  const controls = el("section", "story-controls vcontrols");
  const back = el("button", "btn icon", "⏮");
  back.title = "Previous frame (←)";
  const play = el("button", "btn primary story-play", "▶ Play");
  const fwd = el("button", "btn icon", "⏭");
  fwd.title = "Next frame (→)";
  const speeds = el("span", "seg");
  const speedBtns = SPEEDS.map((s) => {
    const btn = el("button", undefined, `${s}×`);
    btn.addEventListener("click", () => player.setSpeed(s));
    speeds.append(btn);
    return btn;
  });
  const time = el("span", "story-time mono");
  const chips = el("span", "chips");
  const chipBtns = OVERLAYS.filter((o) => o.id !== "events" || meta.has_event).map((o) => {
    const c = el("button", "chip");
    c.innerHTML = `<kbd>${o.key}</kbd>`;
    c.append(o.label);
    c.setAttribute("aria-label", `${o.label} (key ${o.key})`);
    c.addEventListener("click", () => toggle(o.id));
    chips.append(c);
    return { o, c };
  });
  controls.append(back, play, fwd, speeds, time, chips);
  main.append(controls);

  // Shared timeline.
  const tl = el("section", "panel vtimeline");
  const tlTitle = el("h2", "panel-title", "Who reacted first");
  const canvas = el("canvas");
  canvas.setAttribute("role", "slider");
  canvas.setAttribute("aria-label", "Playback position");
  canvas.tabIndex = 0;
  const objects = el("ol", "story-moments vobjects");
  tl.append(tlTitle, canvas, objects);
  main.append(tl);

  const foot = el("footer", "story-foot");
  const more = el("a", undefined, "Open the detailed view →");
  more.href = `/replay/${encodeURIComponent(run)}`;
  foot.append(more);
  if (idx.synthetic) foot.append(el("span", "muted", "Synthetic footage: shows how the viewer works, not a measurement."));
  else foot.append(el("span", "muted", `Recording: ${meta.source || "DSEC"} · ${meta.frames} frames at ${meta.fps} fps`));
  main.append(foot);

  const labelOf = (n: string) => idx.pipelines.find((p) => p.name === n)?.label ?? n;
  const short = labelOf; // full names: "Panther" and "Panther (no training)" are different contestants

  function headline(): void {
    const p = rx.pair;
    h1.replaceChildren();
    sub.textContent = "";
    if (!p || !rx.objects.length) {
      h1.textContent = idx.title;
      sub.textContent = rx.objects.length ? "" : `This recording has no ${idx.ground_truth.pseudo ? "" : "labelled "}objects in the path, so there is nothing to time reactions against.`;
      return;
    }
    if (p.median_lead_s === null) {
      h1.textContent = `${short(p.a)} and ${short(p.b)} never both reacted to the same object.`;
    } else {
      const ms = Math.round(Math.abs(p.median_lead_s) * 1000);
      h1.append(`${short(p.a)} reacted `);
      h1.append(el("em", undefined, ms === 0 ? "at the same time as" : `${ms} ms ${p.median_lead_s > 0 ? "earlier" : "later"}`));
      h1.append(ms === 0 ? ` ${short(p.b)}.` : ` than ${short(p.b)}.`);
    }
    const what = idx.ground_truth.pseudo ? "object" : "labelled object";
    const parts = [`Median over ${p.both} ${what}${p.both === 1 ? "" : "s"} both caught`];
    if (p.only_a) parts.push(`${short(p.a)} alone caught ${p.only_a}`);
    if (p.only_b) parts.push(`${short(p.b)} alone caught ${p.only_b}`);
    if (p.neither) parts.push(`neither caught ${p.neither}`);
    sub.textContent = `${parts.join(" · ")}.`;
  }

  function renderObjects(): void {
    objects.replaceChildren();
    for (const o of rx.objects) {
      const ra = o.reactions[a];
      const rb = o.reactions[b];
      const li = el("li");
      const btn = el("button");
      let verdict = "neither reacted";
      if (ra !== null && rb !== null) {
        const d = Math.round((rb - ra) * 1000);
        verdict = d === 0 ? "both at once" : `${short(d > 0 ? a : b)} ${Math.abs(d)} ms earlier`;
      } else if (ra !== null) verdict = `only ${short(a)} reacted`;
      else if (rb !== null) verdict = `only ${short(b)} reacted`;
      btn.append(el("span", "mono", `${o.t_gt_s.toFixed(2)} s`), el("span", undefined, `${o.cls} #${o.track_id}: ${verdict}`));
      btn.addEventListener("click", () => player.seek(Math.max(0, o.t_gt_s - 1.5)));
      li.append(btn);
      objects.append(li);
    }
  }

  function drawTimeline(): void {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 800;
    const H = 96;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.height = `${H}px`;
    const ctx = canvas.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const M = { l: 10, r: 10 };
    const x = (t: number) => M.l + (t / duration) * (W - M.l - M.r);
    const rowY = { gt: 18, a: 46, b: 70 };
    ctx.fillStyle = "#182032";
    ctx.fillRect(M.l, rowY.a - 7, W - M.l - M.r, 14);
    ctx.fillRect(M.l, rowY.b - 7, W - M.l - M.r, 14);
    for (const [arm, y] of [[a, rowY.a], [b, rowY.b]] as const) {
      ctx.fillStyle = armColor(arm);
      ctx.globalAlpha = 0.35;
      for (const t of rx.fires[arm] ?? []) ctx.fillRect(x(t) - 1, y - 7, 2, 14);
      ctx.globalAlpha = 1;
    }
    ctx.font = "11px 'IBM Plex Mono', monospace";
    for (const o of rx.objects) {
      ctx.strokeStyle = "rgba(255,255,255,0.75)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x(o.t_gt_s), rowY.gt + 4);
      ctx.lineTo(x(o.t_gt_s), H - 6);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#e6eaf2";
      ctx.fillText(`${o.cls} #${o.track_id}`, x(o.t_gt_s) + 3, rowY.gt);
      for (const [arm, y] of [[a, rowY.a], [b, rowY.b]] as const) {
        const t = o.reactions[arm];
        if (t === null || t === undefined) continue;
        ctx.fillStyle = armColor(arm);
        ctx.beginPath();
        ctx.arc(x(t), y, 6, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
    ctx.fillStyle = "#e6eaf2";
    ctx.fillRect(x(player.t) - 1, 4, 2, H - 8);
  }
  new ResizeObserver(() => drawTimeline()).observe(canvas);
  let dragging = false;
  const seekX = (cx: number) => {
    const r = canvas.getBoundingClientRect();
    player.seek(((cx - r.left - 10) / (r.width - 20)) * duration);
  };
  canvas.addEventListener("pointerdown", (e) => {
    dragging = true;
    canvas.setPointerCapture(e.pointerId);
    seekX(e.clientX);
  });
  canvas.addEventListener("pointermove", (e) => dragging && seekX(e.clientX));
  canvas.addEventListener("pointerup", () => (dragging = false));

  // Behaviour.
  const frameAt = (t: number): number => {
    let lo = 0;
    let hi = meta.t_s.length - 1;
    while (lo < hi) {
      const m = (lo + hi + 1) >> 1;
      if (meta.t_s[m] <= t + 1e-6) lo = m;
      else hi = m - 1;
    }
    return lo;
  };
  const stepFrame = (d: number) => {
    if (player.playing) player.toggle();
    const i = Math.min(meta.frames - 1, Math.max(0, frameAt(player.t) + d));
    player.seek(meta.t_s[i]);
  };
  const toggle = (id: Overlay) => {
    if (show.has(id)) show.delete(id);
    else show.add(id);
    player.invalidate();
  };
  back.addEventListener("click", () => stepFrame(-1));
  fwd.addEventListener("click", () => stepFrame(1));
  play.addEventListener("click", () => player.toggle());
  window.addEventListener("keydown", (e) => {
    const tg = e.target as HTMLElement;
    if (tg.tagName === "SELECT" || tg.tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === " " && tg.tagName !== "BUTTON") {
      e.preventDefault();
      player.toggle();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (e.shiftKey) player.seek(player.t - 1);
      else stepFrame(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (e.shiftKey) player.seek(player.t + 1);
      else stepFrame(1);
    } else {
      const o = OVERLAYS.find((x) => x.key === e.key);
      if (o) toggle(o.id);
    }
  });
  const fps = meta.fps || 20;
  player.canAdvance = (t) => {
    const i = frameAt(t);
    return frames.get(Math.min(meta.frames - 1, i + 1)) !== null && ovs.at(t) !== null;
  };

  headline();
  renderObjects();
  const scaleOf = (arm: string) => idx.pipelines.find((p) => p.name === arm)?.bearing_p99 || 1;
  player.on(() => {
    const t = player.t;
    const i = frameAt(t);
    const ahead = Math.ceil(AHEAD_S * fps * Math.max(1, player.speed));
    frames.want(i, i + ahead, "rgb");
    if (show.has("events")) frames.want(i, i + Math.ceil(ahead / 2), "event");
    const ov = ovs.at(t);
    const img = frames.get(i);
    const ev = show.has("events") ? frames.get(i, "event") : null;
    for (const p of [paneA, paneB]) {
      p.draw({ img, ev, meta, frame: i, ov, show, bearingScale: scaleOf(p.arm) });
    }
    play.textContent = player.playing ? "❚❚ Pause" : "▶ Play";
    time.textContent = `${t.toFixed(2)} s · frame ${i + 1}/${meta.frames}`;
    speedBtns.forEach((btn, k) => btn.setAttribute("aria-pressed", String(SPEEDS[k] === player.speed)));
    for (const { o, c } of chipBtns) c.setAttribute("aria-pressed", String(show.has(o.id)));
    drawTimeline();
    url.set({ t: t.toFixed(2) });
  });
}

/** The simple view: video side by side when the run has footage, the story page otherwise. */
export async function simpleView(root: HTMLElement, run: string, story: (root: HTMLElement, run: string) => Promise<void>): Promise<void> {
  const idx = await api.index(run);
  if (idx.video && idx.video.frames > 0) return videoPage(root, run, idx);
  return story(root, run);
}
