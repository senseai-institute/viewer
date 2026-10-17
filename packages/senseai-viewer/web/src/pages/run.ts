// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Run page (/run): pick recordings and contestants, press Run, watch every contestant work
// through the recording live, then see the result and open the side-by-side video.
// Follows a run through Server-Sent Events of the bench events contract. /run?id=<run> reopens one.

import { runner, type RunnerArm, type RunnerOptions, type RunnerRun } from "../api/client.ts";
import { armColor } from "../arms.ts";
import { el, topbar } from "../components/chrome.ts";

type Ev = Record<string, unknown> & { type: string };

interface ArmState {
  name: string;
  label: string;
  frac: number;
  fires: number;
  p99: number | null;
  state: "waiting" | "running" | "done" | "error";
  error: string | null;
}

interface SeqState {
  name: string;
  duration: number;
  samples: Map<string, [number, number | null][]>;
  fires: Map<string, number[]>;
  gt: { t: number; label: string }[];
}

const LIMITS: [string, number | null][] = [
  ["Whole recording", null],
  ["First 10 s", 10],
  ["First 30 s", 30],
  ["First 60 s", 60],
];

function fmtDelay(d: unknown): string {
  if (typeof d !== "number") return "—";
  const ms = Math.round(d * 1000);
  if (ms === 0) return "on time";
  return ms < 0 ? `${-ms} ms before` : `${ms} ms after`;
}

function pct(v: unknown): string {
  return typeof v === "number" ? `${Math.round(v * 100)}%` : "—";
}

function num(v: unknown, digits = 1, unit = ""): string {
  return typeof v === "number" ? `${v.toFixed(digits)}${unit}` : "—";
}

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export async function runPage(root: HTMLElement): Promise<void> {
  document.title = "Run · SenseAI";
  root.replaceChildren(topbar({ current: "run", title: "Run the comparison" }));
  const main = el("main", "runpage");
  root.append(main);
  let opts: RunnerOptions = await runner.options();
  if (!opts.enabled) {
    const box = el("section", "panel");
    box.append(
      el("h1", undefined, "Running is switched off here"),
      el("p", "muted", "Start the viewer with SENSEAI_RUNNER=1 on the machine that has the recordings, then reload this page. The README section “Run the POC from the browser (EC2)” has the steps."),
    );
    main.append(box);
    return;
  }

  const head = el("div", "hero-block");
  head.append(
    el("h1", undefined, "Which one reacts first?"),
    el("p", undefined, "Pick one or more night recordings and the contestants, then press Run. You will see each contestant work through the recording live, then the result and the video side by side."),
  );
  main.append(head);

  // ---------------------------------------------------------------- pickers
  const pick = el("div", "run-pick");
  const seqBox = el("section", "panel");
  seqBox.append(el("h2", "panel-title", "1 · Recordings (darkest first)"));
  const seqList = el("div", "pick-list");
  const seqNote = el("p", "muted small");
  seqBox.append(seqList, seqNote);
  const armBox = el("section", "panel");
  armBox.append(el("h2", "panel-title", "2 · Contestants"));
  const armList = el("div", "pick-list");
  armBox.append(armList);
  pick.append(seqBox, armBox);
  main.append(pick);

  const go = el("section", "run-go");
  const limit = el("select");
  limit.setAttribute("aria-label", "How much of each recording");
  for (const [l, v] of LIMITS) limit.append(new Option(l, v === null ? "" : String(v)));
  limit.value = "30";
  const runBtn = el("button", "btn primary run-btn", "▶ Run");
  const goNote = el("span", "muted");
  go.append(runBtn, limit, goNote);
  main.append(go);

  const chosenSeq = new Set<string>();
  const chosenArm = new Set<string>();

  const renderSequences = () => {
    seqList.replaceChildren();
    const seqs = opts.sequences ?? [];
    if (!seqs.length) {
      seqList.append(el("p", "muted", opts.data_root ? `No DSEC recordings found under ${opts.data_root}.` : "SENSEAI_DATA_ROOT is not set on the server."));
    }
    if (!chosenSeq.size && seqs.length) chosenSeq.add((seqs.find((s) => s.labels) ?? seqs[0]).path);
    for (const s of seqs) {
      const lab = el("label", "pick");
      const cb = el("input");
      cb.type = "checkbox";
      cb.checked = chosenSeq.has(s.path);
      cb.addEventListener("change", () => {
        if (cb.checked) chosenSeq.add(s.path);
        else chosenSeq.delete(s.path);
        refreshGo();
      });
      const body = el("span", "pick-body");
      const title = el("span", "pick-title", s.name);
      if (s.night) title.append(el("span", "badge teal", "night"));
      if (s.labels) title.append(el("span", "badge ok", "labels ✓"));
      const facts = `${s.duration_s.toFixed(0)} s · ${s.frames} frames · ${s.brightness === null ? "measuring brightness…" : `brightness ${s.brightness.toFixed(0)}/255`}`;
      body.append(title, el("span", "pick-sub", facts));
      lab.append(cb, body);
      seqList.append(lab);
    }
    seqNote.textContent = seqs.some((s) => !s.labels)
      ? "Recordings without labels can still run, but there is nothing to score reaction times against."
      : "";
  };

  const renderArms = () => {
    armList.replaceChildren();
    const arms = opts.arms ?? [];
    if (!chosenArm.size) {
      for (const a of arms) if (a.available && (a.name === "panther_prior" || a.name === "panther_vision" || a.name === "torchvision")) chosenArm.add(a.name);
    }
    for (const a of arms) armList.append(armRow(a));
  };

  const armRow = (a: RunnerArm) => {
    const lab = el("label", `pick${a.available ? "" : " off"}`);
    const cb = el("input");
    cb.type = "checkbox";
    cb.disabled = !a.available;
    cb.checked = a.available && chosenArm.has(a.name);
    cb.addEventListener("change", () => {
      if (cb.checked) chosenArm.add(a.name);
      else chosenArm.delete(a.name);
      refreshGo();
    });
    const sw = el("span", "swatch");
    sw.style.background = armColor(a.name);
    const body = el("span", "pick-body");
    const title = el("span", "pick-title");
    title.append(sw, a.label);
    body.append(title, el("span", "pick-sub", a.available ? a.blurb : `Not available here: ${a.reason}.`));
    lab.append(cb, body);
    return lab;
  };

  const refreshGo = () => {
    const busy = opts.busy;
    runBtn.disabled = !!busy || !chosenSeq.size || !chosenArm.size || opts.bench_found === false;
    goNote.textContent = busy
      ? "A run is already going (shown below)."
      : opts.bench_found === false
        ? "senseai-bench is not installed on the server."
        : `${chosenSeq.size} recording${chosenSeq.size === 1 ? "" : "s"} × ${chosenArm.size} contestant${chosenArm.size === 1 ? "" : "s"}`;
  };

  renderSequences();
  renderArms();
  refreshGo();
  const pollBrightness = async () => {
    while (opts.brightness_pending) {
      await new Promise((r) => setTimeout(r, 1500));
      opts = await runner.options();
      renderSequences();
    }
  };
  void pollBrightness();

  // ---------------------------------------------------------------- live view
  const live = el("section", "run-live");
  main.append(live);
  const histBox = el("section", "panel");
  histBox.append(el("h2", "panel-title", "Earlier runs"));
  const hist = el("div", "run-history");
  histBox.append(hist);
  main.append(histBox);

  let source: EventSource | null = null;

  const follow = (run: RunnerRun) => {
    source?.close();
    history.replaceState(null, "", `/run?id=${encodeURIComponent(run.id)}`);
    live.replaceChildren();
    const panel = el("section", "panel");
    const status = el("p", "run-status", "Starting…");
    status.setAttribute("aria-live", "polite");
    const cancel = el("button", "btn", "Stop");
    const top = el("div", "run-top");
    top.append(status, cancel);
    const cards = el("div", "arm-cards");
    const tabs = el("div", "seg");
    const chartWrap = el("div", "run-chart");
    const canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "Live chart: how alarmed each contestant is over the recording, with reactions and labelled objects");
    const legend = el("div", "legend");
    chartWrap.append(canvas, legend);
    const result = el("div", "run-result");
    panel.append(top, cards, tabs, chartWrap, result);
    live.append(panel);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      await runner.cancel(run.id).catch(() => undefined);
    });

    const arms = new Map<string, ArmState>();
    const seqs = new Map<string, SeqState>();
    const summaries: { arm: string; seq: string; s: Record<string, unknown>; error: unknown }[] = [];
    let shown: string | null = null;
    let current: { seq: string; arm: string } | null = null;

    const armCard = new Map<string, { root: HTMLElement; bar: HTMLElement; line: HTMLElement; state: HTMLElement }>();
    const ensureCard = (a: ArmState) => {
      let c = armCard.get(a.name);
      if (!c) {
        const root2 = el("div", "arm-card");
        root2.style.borderTopColor = armColor(a.name);
        const name = el("div", "arm-name", a.label);
        const state = el("div", "arm-state", "waiting");
        const barWrap = el("div", "bar");
        const bar = el("i");
        bar.style.background = armColor(a.name);
        barWrap.append(bar);
        const line = el("div", "arm-line mono");
        root2.append(name, state, barWrap, line);
        cards.append(root2);
        c = { root: root2, bar, line, state };
        armCard.set(a.name, c);
      }
      c.bar.style.width = `${Math.round(a.frac * 100)}%`;
      c.line.textContent = `${a.fires} reaction${a.fires === 1 ? "" : "s"} · p99 ${a.p99 === null ? "—" : `${a.p99.toFixed(1)} ms`}`;
      c.state.textContent = a.state === "error" ? `error: ${a.error ?? ""}` : a.state;
      c.state.className = `arm-state ${a.state}`;
    };

    const renderTabs = () => {
      tabs.replaceChildren();
      if (seqs.size < 2) return;
      for (const name of seqs.keys()) {
        const b = el("button", undefined, name);
        b.setAttribute("aria-pressed", String(name === shown));
        b.addEventListener("click", () => {
          shown = name;
          renderTabs();
          draw();
        });
        tabs.append(b);
      }
    };

    const draw = () => {
      const s = shown ? seqs.get(shown) : undefined;
      const dpr = window.devicePixelRatio || 1;
      const W = canvas.clientWidth || 800;
      const H = 220;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      canvas.style.height = `${H}px`;
      const ctx = canvas.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!s) return;
      const M = { l: 12, r: 12, t: 22, b: 22 };
      const dur = Math.max(1, s.duration);
      const x = (t: number) => M.l + (t / dur) * (W - M.l - M.r);
      const ph = H - M.t - M.b;
      ctx.strokeStyle = "#243049";
      ctx.beginPath();
      ctx.moveTo(M.l, M.t + ph + 0.5);
      ctx.lineTo(W - M.r, M.t + ph + 0.5);
      ctx.stroke();
      ctx.fillStyle = "#6b7690";
      ctx.font = "11px 'IBM Plex Mono', monospace";
      const step = dur > 120 ? 30 : dur > 40 ? 10 : 5;
      for (let t = 0; t <= dur + 1e-6; t += step) ctx.fillText(`${t}s`, x(t) - 6, H - 6);
      for (const g of s.gt) {
        ctx.strokeStyle = "rgba(255,255,255,0.7)";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x(g.t), M.t);
        ctx.lineTo(x(g.t), M.t + ph);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#e6eaf2";
        ctx.fillText(g.label, x(g.t) + 4, M.t - 8);
      }
      for (const [arm, pts] of s.samples) {
        const vals = pts.map((p) => p[1]).filter((v): v is number => v !== null);
        const vmax = Math.max(1e-6, ...vals);
        ctx.strokeStyle = armColor(arm);
        ctx.lineWidth = 1.75;
        ctx.beginPath();
        let started = false;
        for (const [t, v] of pts) {
          if (v === null) {
            started = false;
            continue;
          }
          const y = M.t + ph - (v / vmax) * ph * 0.9;
          if (started) ctx.lineTo(x(t), y);
          else ctx.moveTo(x(t), y);
          started = true;
        }
        ctx.stroke();
        for (const t of s.fires.get(arm) ?? []) {
          ctx.fillStyle = armColor(arm);
          ctx.beginPath();
          ctx.moveTo(x(t) - 6, M.t + ph + 2);
          ctx.lineTo(x(t) + 6, M.t + ph + 2);
          ctx.lineTo(x(t), M.t + ph - 9);
          ctx.closePath();
          ctx.fill();
        }
      }
      if (current && current.seq === shown) {
        const a = arms.get(current.arm);
        const pts = s.samples.get(current.arm);
        const t = pts?.length ? pts[pts.length - 1][0] : 0;
        if (a && a.state === "running") {
          ctx.fillStyle = "#e6eaf2";
          ctx.fillRect(x(t) - 1, M.t, 2, ph);
        }
      }
      legend.replaceChildren();
      for (const arm of s.samples.keys()) {
        const it = el("span");
        const sw = el("i");
        sw.style.background = armColor(arm);
        it.append(sw, arms.get(arm)?.label ?? arm);
        legend.append(it);
      }
      const note = el("span", undefined, "Each line is scaled to its own peak. ▲ = reaction · dashed = a labelled object enters the path");
      legend.append(note);
    };
    new ResizeObserver(() => draw()).observe(canvas);

    const showResult = async (ev: Ev) => {
      cancel.remove();
      result.replaceChildren();
      const err = ev.error as string | null;
      const headline = el("h2", "run-headline", (ev.headline as string) || (err ? "The run did not finish" : "Run finished"));
      result.append(headline);
      if (err) result.append(el("p", "error", err));
      const byArm = new Map<string, typeof summaries>();
      for (const s of summaries) byArm.set(s.arm, [...(byArm.get(s.arm) ?? []), s]);
      if (byArm.size) {
        const table = el("table", "grid results");
        table.innerHTML = "<thead><tr><th>Contestant</th><th>Objects caught</th><th>Reaction (median)</th><th>False alarms / hour</th><th>Latency p99</th></tr></thead>";
        const tb = el("tbody");
        for (const [arm, rows] of byArm) {
          const ok = rows.filter((r) => !r.error);
          const nums = (k: string) => ok.map((r) => r.s[k]).filter((v): v is number => typeof v === "number");
          const hit = nums("hit_rate");
          const tr = el("tr");
          const name = el("td");
          const sw = el("span", "swatch");
          sw.style.background = armColor(arm);
          name.append(sw, arms.get(arm)?.label ?? arm);
          const err2 = rows.find((r) => r.error);
          tr.append(
            name,
            el("td", "mono", pct(hit.length ? hit.reduce((a, b) => a + b, 0) / hit.length : null)),
            el("td", "mono", fmtDelay(median(nums("median_delay_s")))),
            el("td", "mono", num(median(nums("unattributed_fires_per_h")), 1)),
            el("td", "mono", num(nums("latency_p99_ms").length ? Math.max(...nums("latency_p99_ms")) : null, 1, " ms")),
          );
          if (err2) tr.title = `error on ${err2.seq}: ${String(err2.error)}`;
          tb.append(tr);
        }
        table.append(tb);
        result.append(table);
        const n = new Set(summaries.map((s) => s.seq)).size;
        result.append(el("p", "muted small", `“Reaction” is how long after a labelled object entered the path the contestant first reacted (before = earlier than that). ${n > 1 ? `Averaged over ${n} recordings.` : ""}`));
      }
      const watch = el("div", "run-watch");
      result.append(watch);
      // The RunLogs are linked once the process has exited; poll briefly for them.
      for (let i = 0; i < 40; i++) {
        const m = await runner.get(run.id).catch(() => null);
        if (m && !m.active) {
          for (const id of m.runlog_ids) {
            const a = el("a", "btn primary", m.runlog_ids.length > 1 ? `Watch side by side · ${id.replace(/^synthetic-video-/, "")}` : "Watch side by side");
            a.href = `/s/${encodeURIComponent(id)}`;
            watch.append(a);
          }
          if (!m.runlog_ids.length && !err) watch.append(el("p", "muted", "This run wrote no RunLog, so there is no video to watch."));
          break;
        }
        await new Promise((r) => setTimeout(r, 500));
      }
      void loadHistory();
    };

    const onEvent = (ev: Ev) => {
      const seq = ev.sequence as string | undefined;
      const arm = ev.arm as string | undefined;
      switch (ev.type) {
        case "run_start": {
          for (const s of (ev.sequences as { name: string; duration_s: number }[]) ?? []) {
            seqs.set(s.name, { name: s.name, duration: s.duration_s, samples: new Map(), fires: new Map(), gt: [] });
          }
          for (const a of (ev.arms as { name: string; label: string }[]) ?? []) {
            const label = opts.arms?.find((x) => x.name === a.name)?.label ?? a.label ?? a.name;
            const st: ArmState = { name: a.name, label, frac: 0, fires: 0, p99: null, state: "waiting", error: null };
            arms.set(a.name, st);
            ensureCard(st);
          }
          shown = seqs.keys().next().value ?? null;
          renderTabs();
          status.textContent = `Running ${arms.size} contestant${arms.size === 1 ? "" : "s"} on ${seqs.size} recording${seqs.size === 1 ? "" : "s"}…`;
          break;
        }
        case "gt": {
          const s = seq ? seqs.get(seq) : undefined;
          s?.gt.push({ t: ev.t_rel_s as number, label: `${String(ev.cls ?? "object")} #${String(ev.track_id ?? "")}` });
          break;
        }
        case "arm_start": {
          if (!seq || !arm) break;
          current = { seq, arm };
          const a = arms.get(arm);
          if (a) {
            a.state = "running";
            a.frac = 0;
            ensureCard(a);
          }
          if (shown !== seq) {
            shown = seq;
            renderTabs();
          }
          status.textContent = `Now: ${a?.label ?? arm} on ${seq}`;
          break;
        }
        case "progress": {
          const a = arm ? arms.get(arm) : undefined;
          if (a) {
            a.frac = Number(ev.frac ?? 0);
            a.fires = Number(ev.fires ?? a.fires);
            a.p99 = (ev.latency_p99_ms as number | null) ?? a.p99;
            ensureCard(a);
            status.textContent = `Now: ${a.label} on ${seq} · ${Math.round(a.frac * 100)}%`;
          }
          break;
        }
        case "sample": {
          const s = seq ? seqs.get(seq) : undefined;
          if (s && arm) {
            const pts = s.samples.get(arm) ?? [];
            pts.push([ev.t_rel_s as number, (ev.value as number | null) ?? null]);
            s.samples.set(arm, pts);
          }
          break;
        }
        case "fire": {
          const s = seq ? seqs.get(seq) : undefined;
          if (s && arm) s.fires.set(arm, [...(s.fires.get(arm) ?? []), ev.t_rel_s as number]);
          break;
        }
        case "arm_end": {
          const a = arm ? arms.get(arm) : undefined;
          if (a) {
            a.frac = 1;
            a.state = ev.error ? "error" : "done";
            a.error = (ev.error as string | null) ?? null;
            ensureCard(a);
          }
          if (arm && seq) summaries.push({ arm, seq, s: (ev.summary as Record<string, unknown>) ?? {}, error: ev.error });
          break;
        }
        case "run_end": {
          source?.close();
          status.textContent = ev.error ? "Stopped." : "Done.";
          void showResult(ev);
          break;
        }
        default:
          break; // producers may add event types; ignore them
      }
      if (ev.type !== "progress") draw();
    };

    source = new EventSource(runner.eventsUrl(run.id));
    source.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data) as Ev);
      } catch {
        /* a malformed line is skipped */
      }
    };
    source.onerror = () => {
      if (source && source.readyState === EventSource.CLOSED) status.textContent = "Lost the connection to the server. Reload to reconnect.";
    };
  };

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    goNote.textContent = "Starting…";
    try {
      const max = limit.value ? Number(limit.value) : null;
      const run = await runner.start([...chosenSeq], [...chosenArm], max);
      opts = { ...opts, busy: run.status === "running" ? run.id : null };
      refreshGo();
      follow(run);
      live.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      goNote.textContent = e instanceof Error ? e.message : String(e);
      runBtn.disabled = false;
    }
  });

  const loadHistory = async () => {
    const runs = await runner.history().catch(() => [] as RunnerRun[]);
    hist.replaceChildren();
    if (!runs.length) hist.append(el("p", "muted", "No runs yet."));
    for (const r of runs.slice(0, 20)) {
      const a = el("a", "run-row");
      a.href = `/run?id=${encodeURIComponent(r.id)}`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        follow(r);
      });
      const badge = el("span", `badge ${r.status === "finished" ? "ok" : r.status === "running" ? "teal" : "alert"}`, r.status);
      a.append(
        el("span", "mono", r.created.replace("T", " ").replace("Z", "")),
        badge,
        el("span", undefined, `${r.sequences.join(", ")} · ${r.arms.length} contestants`),
        el("span", "muted", r.headline || r.error || ""),
      );
      hist.append(a);
    }
    opts = { ...opts, busy: runs.find((r) => r.active)?.id ?? null };
    refreshGo();
  };
  await loadHistory();

  const want = new URLSearchParams(location.search).get("id") ?? opts.busy ?? null;
  if (want) {
    const r = await runner.get(want).catch(() => null);
    if (r) follow(r);
  }
}
