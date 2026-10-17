// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Run page (/run): one click from the recordings in S3 to a Panther report.
//   Setup     is the software installed, is there a GPU, can this machine read S3 and GitHub
//   Data      the DSEC recordings in S3 and on disk; Fetch & prepare gets them ready
//   Run       train / test recordings, model size, steps, contestants, Run POC
//   Progress  the five steps, log tail, time, GPU / CPU, Cancel / Resume
//   Results   head-to-head, side-by-side video, Compare, Share, and every earlier run
// Everything lives on the server (jobs keep running when the browser or SSH tunnel drops);
// this page only polls. /run?job=<id> reopens one run.

import {
  run,
  type DataIndex,
  type DataRow,
  type Job,
  type RunArm,
  type RunOptions,
  type SetupChecks,
  type Summary,
} from "../api/client.ts";
import { armColor } from "../arms.ts";
import { copyLink, el, topbar } from "../components/chrome.ts";
import { bytes, eta, span } from "../format.ts";

const POLL_MS = 2000;
const LIMITS: [string, number | null][] = [
  ["Whole recording", null],
  ["First 30 s", 30],
  ["First 60 s", 60],
  ["First 5 min", 300],
];
const STATUS_TEXT: Record<string, string> = {
  in_s3: "in S3",
  local: "on disk",
  preparing: "preparing",
  downloading: "downloading",
  downloaded: "downloaded",
  unpacking: "unpacking",
  arranging: "arranging",
  validating: "checking",
  ready: "ready",
  problem: "needs attention",
  failed: "failed",
};

const secs = (from: string | null, to: string | null = null): number =>
  from ? Math.max(0, ((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000) : 0;

function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function reaction(d: number | null): string {
  if (d === null) return "no reaction";
  const ms = Math.round(d * 1000);
  if (ms === 0) return "on time";
  return ms < 0 ? `${-ms} ms early` : `${ms} ms late`;
}

function button(label: string, cls = "btn", onClick?: () => void): HTMLButtonElement {
  const b = el("button", cls, label);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function panel(title: string, cls = ""): { box: HTMLElement; head: HTMLElement; body: HTMLElement } {
  const box = el("section", `panel run-panel ${cls}`.trim());
  const head = el("h2", "panel-title", title);
  const body = el("div", "run-panel-body");
  box.append(head, body);
  return { box, head, body };
}

export async function runPage(root: HTMLElement): Promise<void> {
  document.title = "Run · SenseAI";
  root.replaceChildren(topbar({ current: "run", title: "Run the POC" }));
  const main = el("main", "runpage");
  root.append(main);
  const enabled = await run.status().then((s) => s.enabled, () => false);
  if (!enabled) {
    const box = el("section", "panel");
    box.append(
      el("h1", undefined, "Running is switched off here"),
      el("p", "muted", "Start the viewer with SENSEAI_RUNNER=1 on the GPU machine (deploy/senseai-viewer.service does this), open the SSH tunnel, then reload this page. docs/one-click.md has the five steps."),
    );
    main.append(box);
    return;
  }

  const head = el("div", "hero-block");
  head.append(
    el("h1", undefined, "Which one reacts first?"),
    el("p", undefined, "Get the night recordings ready, pick what to train and test on, press Run POC. The model is trained, packaged and raced against standard camera AI, and the result opens as side-by-side video. Closing this page does not stop a run."),
  );
  const setupP = panel("Setup", "run-setup");
  const dataP = panel("1 · Recordings");
  const runP = panel("2 · Run");
  const progP = panel("3 · Progress", "run-progress");
  const resP = panel("4 · Results");
  main.append(head, setupP.box, dataP.box, runP.box, progP.box, resP.box);

  let data: DataIndex | null = null;
  let opts: RunOptions | null = null;
  let checks: SetupChecks | null = null;
  let job: Job | null = null;
  let history: Job[] = [];

  // ================================================================ Setup
  const setupList = el("div", "checks");
  const setupFix = el("div", "check-fix");
  const setupBtns = el("div", "run-go");
  const installNote = el("span", "muted");
  const installLog = el("pre", "log hidden");
  const checkBtn = button("Check again", "btn", () => void loadSetup(true));
  const installBtn = button("Install / repair", "btn primary", async () => {
    installBtn.disabled = true;
    try {
      const j = await run.install();
      installNote.textContent = "Installing… (this can take 10–20 minutes the first time)";
      void followInstall(j.id);
    } catch (e) {
      installNote.textContent = e instanceof Error ? e.message : String(e);
      installBtn.disabled = false;
    }
  });
  setupBtns.append(checkBtn, installBtn, installNote);
  setupP.body.append(setupList, setupFix, setupBtns, installLog);
  const setupSummary = el("span", "badge");
  setupP.head.append(el("span", "spacer"), setupSummary);

  const CHECKS: [keyof SetupChecks, string][] = [
    ["stack", "SenseAI software"],
    ["gpu", "GPU"],
    ["s3", "Recordings bucket (S3)"],
    ["github", "GitHub (to install and update)"],
    ["data_root", "Recordings folder"],
  ];

  const renderSetup = () => {
    if (!checks) return;
    setupList.replaceChildren();
    for (const [k, label] of CHECKS) {
      const c = checks[k] as { ok: boolean; message: string };
      const row = el("div", `check ${c.ok ? "ok" : k === "gpu" ? "warn" : "bad"}`);
      row.append(el("span", "check-mark", c.ok ? "✓" : k === "gpu" ? "!" : "✗"), el("span", "check-name", label), el("span", "check-msg", c.message));
      setupList.append(row);
    }
    setupFix.replaceChildren();
    if (checks.s3.policy) {
      const pre = el("pre", "policy mono", checks.s3.policy);
      const copy = button("Copy policy", "btn", () => {
        void copyLink(checks?.s3.policy ?? "").then((ok) => (copy.textContent = ok ? "Copied" : "Select the text and copy it"));
      });
      setupFix.append(el("p", undefined, checks.s3.fix ?? "Add this policy to the instance role:"), pre, copy);
    }
    setupSummary.textContent = checks.all_ok ? "ready" : "needs attention";
    setupSummary.className = `badge ${checks.all_ok ? "ok" : "warn"}`;
    setupP.box.classList.toggle("collapsed", checks.all_ok && !installLog.textContent);
    installBtn.disabled = !!checks.install_job;
    if (checks.install_job && !installLog.textContent) void followInstall(checks.install_job);
  };

  const loadSetup = async (refresh = false) => {
    checkBtn.disabled = true;
    checks = await run.setup(refresh).catch(() => checks);
    checkBtn.disabled = false;
    renderSetup();
  };

  let followingInstall: string | null = null;
  const followInstall = async (id: string) => {
    if (followingInstall === id) return;
    followingInstall = id;
    installLog.classList.remove("hidden");
    setupP.box.classList.remove("collapsed");
    for (;;) {
      const [j, log] = await Promise.all([run.job(id).catch(() => null), run.log(id, "install", 30).catch(() => null)]);
      if (log) installLog.textContent = log.lines.join("\n");
      installLog.scrollTop = installLog.scrollHeight;
      if (!j) break;
      installNote.textContent = j.active ? j.stages[0]?.msg || "Installing…" : j.status === "succeeded" ? "Installed." : `Install stopped: ${j.error ?? j.status}`;
      if (!j.active) break;
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
    followingInstall = null;
    installBtn.disabled = false;
    await loadSetup(true);
    opts = await run.options().catch(() => opts);
    renderRun();
  };

  // ================================================================ Data
  const s3Line = el("p", "muted small");
  const dataTable = el("table", "grid data");
  const dataBtns = el("div", "run-go");
  const dataNote = el("span", "muted");
  const picked = new Set<string>();
  const prepBtn = button("Fetch & prepare selected", "btn primary", () => void prepare([...picked]));
  dataBtns.append(prepBtn, button("Refresh", "btn", () => void loadData(true)), dataNote);
  dataP.body.append(s3Line, dataTable, dataBtns);

  const prepare = async (names: string[]) => {
    if (!names.length) return;
    prepBtn.disabled = true;
    dataNote.textContent = "Starting…";
    try {
      const r = await run.prepare(names);
      dataNote.textContent = [
        r.started.length ? `Preparing ${r.started.length}.` : "",
        ...r.skipped.map((s) => `${s.sequence}: ${s.why}.`),
      ].filter(Boolean).join(" ");
      picked.clear();
    } catch (e) {
      dataNote.textContent = e instanceof Error ? e.message : String(e);
    }
    await loadData();
  };

  const statusCell = (r: DataRow) => {
    const td = el("td");
    const busy = !!r.job;
    let text = STATUS_TEXT[r.status] ?? r.status;
    if (busy && r.job?.frac !== null && r.job?.frac !== undefined) text += ` ${Math.round(r.job.frac * 100)}%`;
    const cls = r.status === "ready" ? "ok" : r.status === "problem" || r.status === "failed" ? "alert" : busy ? "teal" : "";
    td.append(el("span", `badge ${cls}`, text));
    if (busy) {
      const bar = el("div", "bar thin");
      const i = el("i");
      i.style.width = `${Math.round((r.job?.frac ?? 0) * 100)}%`;
      bar.append(i);
      td.append(bar);
    }
    return td;
  };

  const renderData = () => {
    if (!data) return;
    s3Line.textContent = `${data.s3.ok ? "✓" : "✗"} ${data.s3.message} · on disk: ${data.root}`;
    s3Line.className = `small ${data.s3.ok ? "muted" : "error"}`;
    dataTable.replaceChildren();
    const thead = el("thead");
    thead.innerHTML = "<tr><th></th><th>Recording</th><th>Status</th><th>Size</th><th>Brightness</th><th>Labels</th><th>Events</th><th></th></tr>";
    const tb = el("tbody");
    if (!data.sequences.length) {
      const tr = el("tr");
      const td = el("td", "muted", "No recordings found in S3 or on disk yet. Check Setup above.");
      td.colSpan = 8;
      tr.append(td);
      tb.append(tr);
    }
    for (const r of data.sequences) {
      const tr = el("tr", "data-row");
      tr.dataset.seq = r.name;
      const pick = el("td");
      const cb = el("input");
      cb.type = "checkbox";
      cb.setAttribute("aria-label", `Select ${r.name}`);
      cb.disabled = r.status === "ready" || !!r.job;
      cb.checked = picked.has(r.name);
      cb.addEventListener("change", () => {
        if (cb.checked) picked.add(r.name);
        else picked.delete(r.name);
        prepBtn.disabled = !picked.size;
      });
      pick.append(cb);
      const name = el("td", "mono", r.name);
      if (r.night) name.append(" ", el("span", "badge teal", "night"));
      const act = el("td");
      if (r.status !== "ready" && !r.job) act.append(button(r.status === "problem" || r.status === "failed" ? "Try again" : "Fetch & prepare", "btn small", () => void prepare([r.name])));
      tr.append(
        pick,
        name,
        statusCell(r),
        el("td", "mono", bytes(r.size_bytes)),
        el("td", "mono", r.brightness === null ? "—" : `${r.brightness.toFixed(0)}/255`),
        el("td", undefined, r.labels ? "✓" : r.status === "ready" ? "none" : "—"),
        el("td", undefined, r.events ? "✓" : "—"),
        act,
      );
      tb.append(tr);
      const notes = [...r.problems.map((p) => ["error", p]), ...(r.error ? [["error", r.error]] : []), ...r.warnings.map((w) => ["muted", w])];
      if (notes.length) {
        const nr = el("tr", "data-notes");
        const td = el("td");
        td.colSpan = 8;
        for (const [cls, text] of notes) td.append(el("div", `small ${cls}`, text));
        nr.append(td);
        tb.append(nr);
      }
    }
    dataTable.append(thead, tb);
    prepBtn.disabled = !picked.size;
  };

  let readyKey = "";
  const loadData = async (refresh = false) => {
    data = await run.data(refresh).catch(() => data);
    renderData();
    const key = ready().map((s) => s.name).join(",");
    if (key !== readyKey) renderRun(); // do not rebuild the form under the user's cursor
  };

  // ================================================================ Run
  const train = new Set<string>();
  const test = new Set<string>();
  const arms = new Set<string>();
  let split = false;
  let splitSeq = "";
  let size: "small" | "gate1" = "small";
  let steps: number | null = null;
  let maxSeconds: number | null = null;

  const runSeqs = el("div", "run-seqs");
  const splitBox = el("div", "run-split");
  const sizeRow = el("div", "run-opts");
  const armList = el("div", "pick-list arms");
  const go = el("div", "run-go");
  const runBtn = button("▶ Run POC", "btn primary run-btn");
  const goNote = el("span", "muted");
  go.append(runBtn, goNote);
  runP.body.append(runSeqs, splitBox, sizeRow, el("h3", "sub", "Contestants"), armList, go);

  const ready = () => (data?.sequences ?? []).filter((s) => s.status === "ready");

  const renderRun = () => {
    if (!opts || !data) return;
    const seqs = ready();
    readyKey = seqs.map((s) => s.name).join(",");
    // Drop picks that are no longer ready.
    for (const set of [train, test]) for (const n of [...set]) if (!seqs.some((s) => s.name === n)) set.delete(n);
    if (!arms.size) for (const a of opts.arms) if (a.available && ["panther_vision", "torchvision"].includes(a.name)) arms.add(a.name);

    runSeqs.replaceChildren();
    if (!split) {
      if (!seqs.length) runSeqs.append(el("p", "muted", "No recording is ready yet: Fetch & prepare at least two above (one to train on, one to test on)."));
      else {
        const t = el("table", "grid seqpick");
        t.innerHTML = "<thead><tr><th>Recording</th><th>Train on</th><th>Test on</th><th>Length</th><th>Brightness</th><th>Labels</th></tr></thead>";
        const tb = el("tbody");
        for (const s of seqs) {
          const tr = el("tr");
          const box = (set: Set<string>, other: Set<string>, role: string) => {
            const td = el("td");
            const cb = el("input");
            cb.type = "checkbox";
            cb.className = `role-${role}`;
            cb.setAttribute("aria-label", `${role} on ${s.name}`);
            cb.checked = set.has(s.name);
            cb.addEventListener("change", () => {
              if (cb.checked) {
                set.add(s.name);
                other.delete(s.name); // a recording is never in both
              } else set.delete(s.name);
              renderRun();
            });
            td.append(cb);
            return td;
          };
          tr.append(
            el("td", "mono", s.name),
            box(train, test, "train"),
            box(test, train, "test"),
            el("td", "mono", span(s.duration_s)),
            el("td", "mono", s.brightness === null ? "—" : `${s.brightness.toFixed(0)}/255`),
            el("td", undefined, s.labels ? "✓" : "none"),
          );
          tb.append(tr);
        }
        t.append(tb);
        runSeqs.append(t);
      }
    }

    splitBox.replaceChildren();
    const lab = el("label", "inline");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = split;
    cb.disabled = !opts.split.available;
    cb.addEventListener("change", () => {
      split = cb.checked;
      renderRun();
    });
    lab.append(cb, ` Only one recording: train on the first ${Math.round(opts.split.train_fraction * 100)} %, test on the rest`);
    splitBox.append(lab);
    if (!opts.split.available) splitBox.append(el("p", "muted small", `Not possible yet: ${opts.split.reason}.`));
    if (split) {
      const sel = el("select");
      sel.setAttribute("aria-label", "Recording to split");
      for (const s of seqs) sel.append(new Option(s.name, s.name));
      if (!seqs.some((s) => s.name === splitSeq)) splitSeq = seqs[0]?.name ?? "";
      sel.value = splitSeq;
      sel.addEventListener("change", () => (splitSeq = sel.value));
      splitBox.append(sel, el("p", "warn small", "Warning: training and testing on the same drive flatters the model. The street, the light and the cars are ones it has already seen. Use a separate test recording for anything you show outside the team."));
    }

    sizeRow.replaceChildren();
    const seg = el("div", "seg");
    for (const s of opts.sizes) {
      const b = button(s.label, undefined, () => {
        size = s.name;
        steps = null;
        renderRun();
      });
      b.setAttribute("aria-pressed", String(size === s.name));
      seg.append(b);
    }
    const stepIn = el("input");
    stepIn.type = "number";
    stepIn.min = "10";
    stepIn.step = "100";
    stepIn.className = "steps";
    stepIn.setAttribute("aria-label", "Training steps");
    stepIn.value = String(steps ?? opts.default_steps[size] ?? 2000);
    stepIn.addEventListener("change", () => (steps = Math.max(10, Number(stepIn.value) || 10)));
    const limit = el("select");
    limit.setAttribute("aria-label", "How much of each test recording");
    for (const [l, v] of LIMITS) limit.append(new Option(l, v === null ? "" : String(v)));
    limit.value = maxSeconds === null ? "" : String(maxSeconds);
    limit.addEventListener("change", () => (maxSeconds = limit.value ? Number(limit.value) : null));
    const stepsLab = el("label", "inline", "Steps ");
    stepsLab.append(stepIn);
    const limLab = el("label", "inline", "Test on ");
    limLab.append(limit);
    sizeRow.append(el("span", "muted", "Model"), seg, stepsLab, limLab);

    armList.replaceChildren();
    for (const a of opts.arms) armList.append(armRow(a));
    refreshGo();
  };

  const armRow = (a: RunArm) => {
    const lab = el("label", `pick${a.available ? "" : " off"}`);
    const cb = el("input");
    cb.type = "checkbox";
    cb.disabled = !a.available;
    cb.checked = a.available && arms.has(a.name);
    cb.addEventListener("change", () => {
      if (cb.checked) arms.add(a.name);
      else arms.delete(a.name);
      refreshGo();
    });
    const sw = el("span", "swatch");
    sw.style.background = armColor(a.name);
    const body = el("span", "pick-body");
    const title = el("span", "pick-title");
    title.append(sw, a.label);
    if (a.needs_training) title.append(el("span", "badge teal", "trained here"));
    if (a.benchmark_only) title.append(el("span", "badge warn", "benchmark only · AGPL"));
    body.append(title, el("span", "pick-sub", a.available ? a.blurb : `Not available here: ${a.reason}.`));
    if (a.benchmark_only) body.append(el("span", "pick-sub", "YOLO is AGPL-licensed: used here only to measure against, never shipped in SenseAI."));
    lab.append(cb, body);
    return lab;
  };

  const request = () => {
    const trainSel = split ? [splitSeq] : [...train];
    const testSel = split ? [] : [...test];
    return {
      train: trainSel.filter(Boolean),
      test: testSel,
      split,
      size,
      steps: steps ?? opts?.default_steps[size] ?? 2000,
      arms: [...arms],
      max_seconds: maxSeconds,
    };
  };

  const problem = (): string | null => {
    if (!opts) return "loading…";
    if (opts.busy) return "A run is going (see Progress). One at a time.";
    if (!arms.size) return "Pick at least one contestant.";
    const needs = opts.arms.some((a) => a.needs_training && arms.has(a.name));
    if (split) return splitSeq ? null : "Pick the recording to split.";
    if (!test.size) return "Tick at least one recording to test on.";
    if (needs && !train.size) return "Tick at least one recording to train on (Panther learns from it).";
    return null;
  };

  const refreshGo = () => {
    const p = problem();
    runBtn.disabled = !!p;
    if (p) goNote.textContent = p;
    else {
      const r = request();
      const needs = opts?.arms.some((a) => a.needs_training && arms.has(a.name));
      goNote.textContent = split
        ? `Train and test on ${splitSeq} · ${r.arms.length} contestants`
        : `${needs ? `Train on ${r.train.length}, ` : "No training needed, "}test on ${r.test.length} · ${r.arms.length} contestant${r.arms.length === 1 ? "" : "s"}`;
    }
  };

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    goNote.textContent = "Starting…";
    try {
      const j = await run.start(request());
      if (opts) opts.busy = j.id;
      select(j);
      progP.box.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      goNote.textContent = e instanceof Error ? e.message : String(e);
      runBtn.disabled = false;
    }
  });

  // ================================================================ Progress
  const progTop = el("div", "run-top");
  const progStatus = el("p", "run-status", "No run yet.");
  progStatus.setAttribute("aria-live", "polite");
  const progBtns = el("div", "run-btns");
  progTop.append(progStatus, progBtns);
  const stageBar = el("div", "stages");
  const progFacts = el("div", "run-facts mono");
  const armCards = el("div", "arm-cards");
  const logHead = el("div", "log-head");
  const logBox = el("pre", "log");
  progP.body.append(progTop, stageBar, progFacts, armCards, logHead, logBox);
  let logStage: string | null = null; // null = follow the current step

  const renderProgress = () => {
    progBtns.replaceChildren();
    if (!job) {
      progStatus.textContent = "No run yet. Press Run POC above.";
      stageBar.replaceChildren();
      progFacts.textContent = "";
      logHead.replaceChildren();
      logBox.textContent = "";
      logBox.classList.add("hidden");
      return;
    }
    logBox.classList.remove("hidden");
    const j = job;
    const cur = j.stages.find((s) => s.status === "running") ?? null;
    const word: Record<string, string> = {
      queued: "Starting…",
      running: cur ? `${cur.label}: ${cur.msg || "working…"}` : "Working…",
      cancelling: "Stopping…",
      succeeded: "Done.",
      failed: `Stopped with an error: ${j.error ?? ""}`,
      cancelled: "Cancelled. Press Resume to carry on where it stopped.",
    };
    progStatus.textContent = word[j.status] ?? j.status;
    progStatus.className = `run-status ${j.status}`;
    if (j.active) {
      const c = button("Cancel", "btn", async () => {
        c.disabled = true;
        job = await run.cancel(j.id).catch(() => job);
        renderProgress();
      });
      c.disabled = j.status === "cancelling";
      progBtns.append(c);
    } else if (j.status === "failed" || j.status === "cancelled") {
      const r = button("Resume", "btn primary", async () => {
        r.disabled = true;
        try {
          job = await run.resume(j.id);
          if (opts) opts.busy = j.id;
          refreshGo();
        } catch (e) {
          progStatus.textContent = e instanceof Error ? e.message : String(e);
        }
        renderProgress();
      });
      progBtns.append(r);
    }

    stageBar.replaceChildren();
    for (const s of j.stages) {
      const seg = el("button", `stage ${s.status}`);
      seg.type = "button";
      seg.title = `${s.label}: ${s.status}${s.msg ? ` · ${s.msg}` : ""}. Click to see its log.`;
      const fill = el("i");
      fill.style.width = `${Math.round((s.status === "done" ? 1 : s.status === "running" ? (s.frac ?? 0) : 0) * 100)}%`;
      const t = s.started ? span(secs(s.started, s.ended)) : "";
      seg.append(fill, el("span", "stage-name", s.label), el("span", "stage-sub", s.status === "skipped" ? "not needed" : s.status === "running" ? `${Math.round((s.frac ?? 0) * 100)}% · ${t}` : s.status === "done" ? `✓ ${t}` : s.status));
      seg.addEventListener("click", () => {
        logStage = s.name;
        void pollLog();
      });
      stageBar.append(seg);
    }
    const total = secs(j.started, j.active ? null : j.ended);
    const left = cur ? eta(secs(cur.started), cur.frac) : null;
    progFacts.replaceChildren(
      el("span", undefined, `run ${j.id}`),
      el("span", undefined, `elapsed ${span(total)}`),
      el("span", undefined, left === null ? (cur ? "time left: measuring…" : "") : `this step ≈ ${span(left)} left`),
      el("span", "sys", ""),
    );
  };

  const renderSystem = (s: { cpu: number | null; gpu: number | null; gpu_mem: number | null }) => {
    const box = progFacts.querySelector(".sys");
    if (box) box.textContent = `CPU ${s.cpu === null ? "—" : `${Math.round(s.cpu)}%`} · GPU ${s.gpu === null ? "—" : `${Math.round(s.gpu)}%`}${s.gpu_mem === null ? "" : ` (memory ${Math.round(s.gpu_mem)}%)`}`;
  };

  const pollLog = async () => {
    if (!job) return;
    const log = await run.log(job.id, logStage ?? undefined, 60).catch(() => null);
    if (!log) return;
    logHead.replaceChildren(el("span", "muted small", `Log · ${job.stages.find((s) => s.name === log.stage)?.label ?? log.stage ?? ""}`));
    if (logStage) logHead.append(button("follow current step", "btn small", () => {
      logStage = null;
      void pollLog();
    }));
    const stick = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 8;
    logBox.textContent = log.lines.join("\n") || "(nothing yet)";
    if (stick) logBox.scrollTop = logBox.scrollHeight;
  };

  // Live contestant cards while the comparison runs (bench events contract, via SSE).
  let source: EventSource | null = null;
  let sourceFor: string | null = null;
  const followCompare = (j: Job) => {
    if (sourceFor === j.id) return;
    source?.close();
    sourceFor = j.id;
    armCards.replaceChildren();
    const cards = new Map<string, { bar: HTMLElement; line: HTMLElement; state: HTMLElement }>();
    const card = (arm: string) => {
      let c = cards.get(arm);
      if (!c) {
        const box = el("div", "arm-card");
        box.style.borderTopColor = armColor(arm);
        const state = el("div", "arm-state", "waiting");
        const barWrap = el("div", "bar");
        const bar = el("i");
        bar.style.background = armColor(arm);
        barWrap.append(bar);
        const line = el("div", "arm-line mono");
        box.append(el("div", "arm-name", opts?.arms.find((a) => a.name === arm)?.label ?? arm), state, barWrap, line);
        armCards.append(box);
        c = { bar, line, state };
        cards.set(arm, c);
      }
      return c;
    };
    source = new EventSource(run.eventsUrl(j.id));
    source.onmessage = (m) => {
      let ev: Record<string, unknown>;
      try {
        ev = JSON.parse(m.data) as Record<string, unknown>;
      } catch {
        return;
      }
      const arm = ev.arm as string | undefined;
      if (ev.type === "run_start") for (const a of (ev.arms as { name: string }[]) ?? []) card(a.name);
      else if (ev.type === "arm_start" && arm) {
        const c = card(arm);
        c.state.textContent = `running on ${String(ev.sequence ?? "")}`;
        c.state.className = "arm-state running";
      } else if (ev.type === "progress" && arm) {
        const c = card(arm);
        c.bar.style.width = `${Math.round(Number(ev.frac ?? 0) * 100)}%`;
        c.line.textContent = `${Number(ev.fires ?? 0)} reactions · p99 ${typeof ev.latency_p99_ms === "number" ? `${ev.latency_p99_ms.toFixed(1)} ms` : "—"}`;
      } else if (ev.type === "arm_end" && arm) {
        const c = card(arm);
        c.bar.style.width = "100%";
        c.state.textContent = ev.error ? `error: ${String(ev.error)}` : "done";
        c.state.className = `arm-state ${ev.error ? "error" : "done"}`;
      } else if (ev.type === "run_end") source?.close();
    };
  };

  // ================================================================ Results
  const resNow = el("div", "run-result");
  const histBox = el("div", "run-history");
  resP.body.append(resNow, el("h3", "sub", "All runs"), histBox);

  const renderResult = () => {
    resNow.replaceChildren();
    if (!job || job.status !== "succeeded" || job.kind !== "poc") {
      resNow.append(el("p", "muted", job?.active ? "The result appears here when the run finishes." : "Pick a finished run below, or start one."));
      return;
    }
    const r = job.result ?? {};
    resNow.append(el("h2", "run-headline", r.headline || "Run finished"));
    if (r.trained === false) resNow.append(el("p", "muted small", "No contestant needed training, so the model was not trained on this run."));
    const byArm = new Map<string, Summary[]>();
    for (const s of r.summaries ?? []) byArm.set(s.arm, [...(byArm.get(s.arm) ?? []), s]);
    const tiles = el("div", "tiles");
    const rows = [...byArm].map(([arm, list]) => {
      const ok = list.filter((x) => !x.error);
      const nums = (k: string) => ok.map((x) => x[k]).filter((v): v is number => typeof v === "number");
      const hit = nums("hit_rate");
      return {
        arm,
        error: list.find((x) => x.error)?.error ?? null,
        delay: median(nums("median_delay_s")),
        hit: hit.length ? hit.reduce((a, b) => a + b, 0) / hit.length : null,
        fa: median(nums("unattributed_fires_per_h")),
        p99: nums("latency_p99_ms").length ? Math.max(...nums("latency_p99_ms")) : null,
      };
    });
    const best = Math.min(...rows.map((x) => x.delay ?? Infinity));
    for (const x of rows) {
      const t = el("div", `tile${x.delay !== null && x.delay === best && rows.length > 1 ? " best" : ""}`);
      t.style.borderTopColor = armColor(x.arm);
      t.append(
        el("div", "tile-name", opts?.arms.find((a) => a.name === x.arm)?.label ?? x.arm),
        el("div", "tile-big", x.error ? "error" : reaction(x.delay)),
        el("div", "tile-sub", x.error ? String(x.error) : `caught ${x.hit === null ? "—" : `${Math.round(x.hit * 100)}%`} · false alarms ${x.fa === null ? "—" : `${x.fa.toFixed(1)}/h`} · p99 ${x.p99 === null ? "—" : `${x.p99.toFixed(1)} ms`}`),
      );
      tiles.append(t);
    }
    if (rows.length) resNow.append(tiles, el("p", "muted small", "Reaction = how long after a labelled object entered the path each contestant first reacted (early = before). Median over the test recordings."));
    const links = el("div", "run-watch");
    const ids = r.runlog_ids ?? [];
    for (const id of ids) {
      const short = ids.length > 1 ? ` · ${id.replace(/^(synthetic-video-|poc-)/, "")}` : "";
      const watch = el("a", "btn primary", `Watch side by side${short}`);
      watch.href = `/s/${encodeURIComponent(id)}`;
      const cmp = el("a", "btn", "Open in Compare");
      cmp.href = `/compare/${encodeURIComponent(id)}`;
      const rep = el("a", "btn", "Replay");
      rep.href = `/replay/${encodeURIComponent(id)}`;
      const share = button("Share", "btn", () => {
        const url = `${location.origin}/s/${encodeURIComponent(id)}`;
        void copyLink(url).then((ok) => (share.textContent = ok ? "Link copied" : url));
      });
      share.title = "Copy a link to the side-by-side video. Through the SSH tunnel only people with the tunnel can open it; with VIEWER_PASSWORD set, anyone with the password.";
      links.append(watch, cmp, rep, share);
    }
    if (r.report_md) {
      const a = el("a", "btn", "Report (Markdown)");
      a.href = run.reportUrl(job.id);
      a.target = "_blank";
      links.append(a);
    }
    if (!ids.length) links.append(el("p", "muted", "This run wrote no RunLog, so there is no video to watch."));
    resNow.append(links);
  };

  const renderHistory = () => {
    histBox.replaceChildren();
    if (!history.length) histBox.append(el("p", "muted", "No runs yet."));
    for (const h of history) {
      const a = el("a", `run-row${job?.id === h.id ? " current" : ""}`);
      a.href = `/run?job=${encodeURIComponent(h.id)}`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        select(h);
      });
      const cls = h.status === "succeeded" ? "ok" : h.active ? "teal" : h.status === "cancelled" ? "warn" : "alert";
      const p = h.params as { train?: string[]; test?: string[]; split?: boolean; arms?: string[] };
      const what = p.split ? `70/30 on ${p.train?.[0] ?? ""}` : `train ${p.train?.join(", ") || "—"} · test ${p.test?.join(", ") ?? ""}`;
      a.append(
        el("span", "mono", h.created.replace("T", " ").replace("Z", "")),
        el("span", `badge ${cls}`, h.status),
        el("span", undefined, `${what} · ${p.arms?.length ?? 0} contestants`),
        el("span", "muted", h.result?.headline || h.error || ""),
      );
      histBox.append(a);
    }
  };

  // ================================================================ wiring
  const select = (j: Job) => {
    if (job?.id !== j.id) {
      logStage = null;
      armCards.replaceChildren();
      source?.close();
      sourceFor = null;
    }
    job = j;
    history = [j, ...history.filter((h) => h.id !== j.id)];
    window.history.replaceState(null, "", `/run?job=${encodeURIComponent(j.id)}`);
    renderProgress();
    renderResult();
    renderHistory();
    void pollLog();
  };

  const tick = async () => {
    try {
      const busyData = data?.sequences.some((s) => s.job) ?? false;
      const tasks: Promise<unknown>[] = [];
      if (busyData) tasks.push(loadData());
      if (job?.active) {
        tasks.push(
          run.job(job.id).then((j) => {
            const finished = job?.active && !j.active;
            job = j;
            if (j.stages.find((s) => s.name === "compare")?.status === "running") followCompare(j);
            renderProgress();
            if (finished) {
              renderResult();
              void Promise.all([run.options(), run.jobs("poc")]).then(([o, h]) => {
                opts = o;
                history = h;
                renderHistory();
                renderRun();
              });
            }
          }),
          pollLog(),
          run.system().then(renderSystem),
        );
      }
      await Promise.all(tasks);
    } catch {
      /* the server may be restarting; try again next tick */
    }
    setTimeout(() => void tick(), POLL_MS);
  };

  // First load: everything in parallel; the slow checks (S3, GitHub) do not block the rest.
  void loadSetup();
  [data, opts, history] = await Promise.all([
    run.data().catch(() => null),
    run.options().catch(() => null),
    run.jobs("poc").catch(() => [] as Job[]),
  ]);
  renderData();
  renderRun();
  renderHistory();
  const want = new URLSearchParams(location.search).get("job") ?? opts?.busy ?? history[0]?.id ?? null;
  if (want) {
    const j = await run.job(want).catch(() => null);
    if (j) select(j);
  } else renderProgress();
  renderResult();
  const first = job as Job | null; // set by select() above
  if (first?.stages.find((s) => s.name === "compare")?.status === "running") followCompare(first);
  void tick();
}
