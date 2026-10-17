# senseai-viewer

The web UX of the SenseAI perception layer. One browser app shows what a SenseAI pipeline
senses, predicts, reacts to and says. It works on a **recorded run** (you upload a log or point
at one) and on a **live endpoint** (a robot running the SDK). The same app is the demo, the
incident-review tool, the benchmark report viewer and the species-library browser.

The viewer reads **only the public contracts**: `AlignedTick`, `TokenBatch`, `SurpriseMap`,
`ReflexCommand`, `Event`, the bench `RunLog`, and the library YAML. Any third-party pipeline that
writes those formats shows up in the viewer exactly like SenseAI's own.

Licence: Apache-2.0. Copyright 2026 SenseAI Institute.

> **Status: design draft, step 1.** Only this README and
> [`server/senseai_viewer/sample_data.py`](server/senseai_viewer/sample_data.py) exist so far.
> Both are up for review before any implementation starts. Everything below describes the
> intended behaviour, and the [window API](#the-window-api) and the
> [RunLog layout](#runlog-layout-v0-what-the-viewer-reads) are the contract the code will be
> tested against. [Open questions](#open-questions-for-review) lists what needs a decision.

---

## Who it is for

**Customers** open a share link and watch their own aisle, at night, the way the robot sees it.
The heat map shows where something unexpected is. The bars show which sense noticed it. The
timeline shows the moment the reflex slowed the robot, and one click explains why in one
sentence. They can compare the Panther profile with the default on the same recording and read
the difference in milliseconds.

**Investors** get the deck's demo as a live page rather than a video. Every number on the screen
(first fire, lead time, false fires, latency p99) is computed by the server from the log,
using the same formulas as `senseai-bench`, so the page can be rerun and checked.

**Biologists** browse the species library without logging in. There is a page per clade with a
stable URL: its organs, thresholds with units, citations as links and conservation status. Each
page also lists which profiles draw on the clade and, in one line, what the robot gets from it.
The page never shows the learned prior values.

**Safety reviewers** jump straight to any reflex fire or cortex event and see the 2 s around it,
with the reflex's own explanation. They can add an annotation and export the window as a JSON
bundle for a safety report.

## What you can do

| # | Page | What it does |
|---|------|--------------|
| 1 | **Replay** | Open a run. Scrub, or play at 0.25× to 4×. If the run holds more than one pipeline, toggle between profiles. Toggle layers: events, frames, surprise grid, bearing ring, reflex path cells and cortex tracks. Hover a cell to see per-head surprise. Click a reflex fire to read its one-sentence `explain`. The URL encodes run, t, layers and profile, so any state can be linked. |
| 2 | **Compare** | Two runs, or two pipelines of one run, side by side on one clock. Head-to-head tiles show first fire, lead time, cells lit, false fires and latency p99. "Swap" exchanges the sides. "Diff" shows the cells lit in A but not in B. |
| 3 | **Live** | Connect to a running SDK endpoint (`/health` plus the telemetry WebSocket). The panels are the same as Replay. Surprise and cortex update at about 10 Hz and health at 1 Hz. A red banner appears when the watchdog degrades. No raw frames are shown unless the endpoint has opted in. |
| 4 | **Incidents** | A table of fires and events, with filters. The detail view shows the ±2 s window, the explain sentence and an annotation thread (role `editor`), and exports the window as JSON. |
| 5 | **Bench** | Open a `senseai-bench` run or comparison report: the lead-time chart, the per-lux breakdown, the false-stop CI and throughput. Links to the leaderboard submission and verifies its hash. |
| 6 | **Library** | The species library from `library/*.yaml`, searchable, with one page per clade at `/library/<clade>`. Public, no login. |
| 7 | **Profiles** | The registry entries, each with its spec (rules, library refs) and its distillation report. The page never shows prior values. |
| 8 | **Fleet** *(Gate 3)* | Many endpoints in one table: health, profile, fires per hour, latency p99. Click a row to open its Live page. |

Any Replay or Compare state can be shared read-only at `/s/{token}`. Share pages need no login,
are rate-limited, and the owner can revoke them.

### The Replay page

The layout follows the Panther replay mockup:

```
┌───────────────────────────────────────────────────────────┬──────────────────────────┐
│ Scene (WebGL)                                             │ Senses                   │
│  night camera underlay, events as 1 px points             │  event ████████░░  7.1   │
│  32×32 surprise grid, ±5 m, teal → amber → red            │  frame ██░░░░░░░░  2.3   │
│  bearing ring (72 bins), reflex path cells outlined       ├──────────────────────────┤
│  cortex tracks: labelled dots with velocity vectors       │ Head to head             │
│  HUD: t · profile · tokens/tick · lux                     │  first fire  6.29 | 6.63 │
│                                                           │  lead time   +0.34 s     │
│                                                           │  cells lit   45 | 21     │
│                                                           │  false fires 0 | 1       │
│                                                           │  e2e p99     11.2 | 10.8 │
├───────────────────────────────────────────────────────────┴──────────────────────────┤
│ Timeline: global surprise vs t (both profiles), reflex threshold, ground truth, fires│
├──────────────────────────────────────────────────────────────────────────────────────┤
│ Transport: ⏮  ▶  0.25× 0.5× 1× 2× 4×   [Panther | default]   layers 1–7   Explain ▸   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

The numbers above are those of the [synthetic sample run](#the-sample-run), with cells lit
counted at Panther's first fire (6.29 s).

### Keyboard

| Key | Action |
|-----|--------|
| `space` | Play or pause |
| `←` / `→` | Step one tick (with `shift`: 1 s) |
| `[` / `]` | Previous / next reflex fire |
| `1`–`7` | Toggle a layer: events, frames, surprise grid, bearing ring, path cells, tracks, ground truth |
| `p` | Cycle profile |
| `e` | Open the explain panel for the fire nearest to t |
| `c` | Toggle the colour-blind-safe scale (viridis-like) |
| `?` | Show shortcuts |

Every control is reachable by keyboard. Text contrast meets WCAG AA on the dark theme.

### Design

Dark first, using the deck's palette: background `#0C111B`, panel `#121826`, accent teal
`#5EEAD4`, warn `#F2B35C`, alert `#FF5A7A`. Type is IBM Plex Sans and IBM Plex Mono. The
layout must stay legible on a projector and collapse to one column on a phone.

---

## Contracts it reads

The server imports none of the pipeline packages at runtime. It reads files and messages
through the schemas below, vendored as `.proto`/JSON-schema copies with their source commit
recorded.

| Contract | Source | State today | What the viewer uses |
|---|---|---|---|
| `AlignedTick`, `Tensor`, tick HDF5 | `senseai-ingest` `schema/sample.proto` | released | tick clock, events, frames, lux |
| `TokenBatch` | `senseai-tokens` `schema/token.proto` | released | tokens per tick, head names |
| `SurpriseMap`, `WorldState` | `senseai-world` `schema/world.proto` (v1) | released | `per_cell` [G, G], `per_bearing` [B], `per_head`, `global_surprise`, `warming_up` |
| profile spec, library YAML | `senseai-profiles` `profile.proto`, `library/schema/clade.schema.json` | released | Library and Profiles pages |
| `Health`, telemetry | `senseai-sdk` `api.py` | design stub | Live page, Fleet |
| `ReflexCommand` (with provenance) | `senseai-reflex` `reflex.proto` | **not started** (empty repo) | action, speed scale, path risk, threshold, path cells, explain |
| `Event` | `senseai-cortex` `cortex.proto` | **not started** (empty repo) | event feed, tracks |
| `RunLog` HDF5, submission JSON | `senseai-bench` | **design draft**: metrics defined, file layout not | everything in a run |

Three of the contracts the viewer depends on do not exist yet. Until they do, the viewer
defines the minimum it needs as **RunLog layout v0** below, using the field names from
`senseai-sdk/api.py` (`ReflexCommand`, `Event`, `Health`) and the metric definitions from
`senseai-bench`. Layout v0 is a proposal to `senseai-bench`, not a fork of it: when bench
publishes its layout, the viewer reads that and v0 becomes a reader shim.

### RunLog layout v0 (what the viewer reads)

A RunLog is one HDF5 file. It holds one **recording**, which is the sensor stream and ground
truth, and one or more **pipelines** that were run on it. A replay log holds several pipelines
on the same recording, which is how Panther and the default share one clock. A bench closed-loop
run holds one pipeline, because the robot's motion depends on it. To compare two closed-loop
runs, open them in Compare.

Times are int64 nanoseconds on the ingest master clock. Tick `k` is the window
`[t0_ns + k·tick_ns, t0_ns + (k+1)·tick_ns)` and is stamped with its end, as in ingest.
`T` is the number of ticks, `G` the grid size and `B` the number of bearing bins. Spatial data
uses world's body frame: x forward, y left, metres, origin at the robot base.

```
/                                   attrs: format="senseai/runlog", layout_version=0, run_id, title,
                                           tick_ns, t0_ns, n_ticks, mode ("replay"|"closed_loop"),
                                           scenario, seed, created_at, synthetic (bool), synthetic_note
/recording/sources/<sid>/           attrs: modality (ingest Modality), width, height, ...
    event:  x u16, y u16, t_ns i64, p i8 (+1/−1)       [n_events], sorted by t_ns
            tick_offsets i64 [T+1]                     events of tick k are [off[k], off[k+1])
    frame:  data u8 [F, H, W] (or [F, H, W, 3]), t_ns i64 [F], tick i64 [F]
/recording/scene/                   lux f32 [T], robot_speed f32 [T], speed_cap f32 [T]
/recording/ground_truth/            attrs: t_gt_ns, t_visible_ns, corridor_margin_m, lookahead_s
    clean_segments_ns i64 [K, 2]    segments where every fire is false (bench "clean")
    actors f32 [T, A, 4]            body-frame x, y, vx, vy per actor
    actor_ids str [A]
/pipelines/<name>/                  attrs: pipeline ("sdk"|"null_profile"|"baseline_rgb"|...),
                                           profile, profile_version, model_id, tokenizer_id
  surprise/                         attrs: grid_cells G, grid_half_extent_m, bearing_bins B, heads
    global f32 [T]                  SurpriseMap.global_surprise
    per_head/<head> f32 [T]         SurpriseMap.per_head
    per_cell f16 [T, G, G]          SurpriseMap.per_cell, indexed [ix, iy] as in world.proto
    per_bearing f16 [T, B]
    warming_up bool [T], num_tokens u32 [T]
  reflex/                           attrs: actions = ["none","slow","brake","swerve","retract","hold"]
    action u8 [T], speed_scale f32 [T], risk f32 [T], threshold f32 [T]
    fires/                          one row per fire (first tick action leaves "none"), columns:
      tick i64, t_ns i64, action str, speed_scale f32, risk f32, threshold f32,
      cells vlen i16 (ix·G + iy), head_shares json, explain str
  cortex/events/                    one row per Event, columns:
      tick, t_ns, kind ("track.new"|"track.update"|"track.lost"|"reflex.fire"|"sensor.degraded"|...),
      track_id, label, x, y, vx, vy, confidence, text
  latency/                          e2e i64 [T] and one i64 [T] per stage (ingest, tokens, world, reflex, sdk)
```

A pipeline may omit any group. The panels that need it then say "not available", and
`senseai-viewer validate` says which panels will light up.

---

## The window API

The browser never opens a log. The server decodes HDF5 lazily and serves **windows**: everything
the panels need for `[t0, t1)`, already downsampled to what a screen can show.

### `GET /api/runs/{run_id}/index`

This is the whole-run overview the timeline and incident list are drawn from. It is computed
**once, at upload**, stored in Postgres, and returned without touching the HDF5 file.

```jsonc
{
  "run_id": "synthetic-panther-vs-null-s7",
  "index_version": 1,
  "tick_ns": 10000000, "t0_ns": "1700000000000000000", "n_ticks": 1200, "duration_s": 12.0,
  "synthetic": true,
  "pipelines": [{"name": "panther", "profile": "panther", "profile_version": 0, "heads": ["event", "frame"]}, ...],
  "layers": {"events": true, "frames": true, "surprise": true, "bearing": true, "reflex": true, "cortex": true, "gt": true},
  "ground_truth": {"t_gt_s": 6.51, "t_visible_s": 5.90, "clean_segments_s": [[0.0, 5.0]]},
  "timeline": {                        // ≤ 4000 buckets per pipeline; exact at ≤ 4000 ticks
    "t_s": [...],                      // bucket end
    "global_max": {"panther": [...], "null": [...]},   // max, so a crossing never disappears
    "risk_max": {"panther": [...], "null": [...]},
    "threshold": {"panther": 2.6, "null": 2.6}
  },
  "fires":  [{"pipeline": "panther", "tick": 628, "t_s": 6.29, "action": "slow", "explain": "slowed to 40% at 6.29 s: ..."}, ...],
  "events": [{"pipeline": "panther", "tick": 620, "t_s": 6.21, "kind": "track.new", "text": "..."}, ...]
}
```

Times in JSON are seconds since `t0_ns`, as float64. Absolute nanosecond timestamps are decimal
strings, because they exceed 2⁵³.

### `GET /api/runs/{run_id}/window`

| Parameter | Default | Meaning |
|---|---|---|
| `t0`, `t1` | required | Window in seconds since `t0_ns`, `0 ≤ t0 < t1`. Snapped outward to tick boundaries. At most 30 s. |
| `pipeline` | all | Comma-separated pipeline names. |
| `layers` | `surprise,reflex,cortex` | Any of `events,frames,surprise,bearing,heads,reflex,cortex,gt,latency`. |
| `max_events` | `50000` | Event points returned, at most 50,000. |
| `frame_w` | `346` | Frame JPEG width in px (≤ source width; aspect preserved). |
| `max_frames` | `40` | Frames returned, evenly spaced over the window. |
| `grid_hz` | `50` | Rate of `per_cell` and `per_bearing` slices, 1–100 Hz (≤ the tick rate). |
| `fmt` | `msgpack` | `msgpack` (`application/msgpack`) or `json` (for debugging and curl). |

The response is a single map:

```jsonc
{
  "window_version": 1,
  "run_id": "...", "t0_s": 6.0, "t1_s": 8.0, "tick0": 600, "tick1": 800,
  "missing": [],                                  // requested layers the log does not have
  "recording": {
    "events": {"source": "events_front", "width": 346, "height": 260,
               "total": 51233, "kept": 50000,       // kept ≤ max_events
               "x": <u16>, "y": <u16>, "t_s": <f32, relative to t0_s>, "p": <i8>},
    "frames": {"source": "rgb_front", "t_s": [..], "tick": [..], "jpeg": [<bytes>, ...], "lux": [..]},
    "gt":     {"t_s": <f32>, "actors": <f32 [n, A, 4]>, "actor_ids": [...]}
  },
  "pipelines": {
    "panther": {
      "surprise": {"t_s": <f32 [n]>, "global": <f32 [n]>, "warming_up": <bool [n]>, "num_tokens": <u32 [n]>,
                   "grid_t_s": <f32 [m]>, "per_cell": <f16 [m, 32, 32]>, "per_bearing": <f16 [m, 72]>},
      "heads":    {"event": <f32 [n]>, "frame": <f32 [n]>},
      "reflex":   {"action": <u8 [n]>, "speed_scale": <f32 [n]>, "risk": <f32 [n]>, "threshold": <f32 [n]>,
                   "fires": [{"tick", "t_s", "action", "speed_scale", "risk", "threshold", "cells", "head_shares", "explain"}]},
      "cortex":   [{"tick", "t_s", "kind", "track_id", "label", "x", "y", "vx", "vy", "confidence", "text"}],
      "latency":  {"e2e_ms": <f32 [n]>}
    }
  }
}
```

In msgpack, every `<dtype [shape]>` is a map `{"dtype": "f2"|"f4"|"u1"|..., "shape": [...],
"data": <bin, little-endian>}`, which the browser wraps in a typed array without copying.
float16 is expanded in the client. In JSON the same arrays are nested lists.

Downsampling is deterministic, so the same request always gives the same bytes:

- **Events.** When the window holds more than `max_events` events, the server keeps every
  `⌈total / max_events⌉`-th event in time order. Per-tick density and polarity balance are
  preserved, and `kept` and `total` let the UI say so.
- **Grid.** `per_cell` is decimated by taking the **max** over each group of ticks, so a hot
  cell that lasts one tick still shows. `per_bearing` is decimated the same way.
- **Per-tick series** (`global`, heads, reflex, latency) are never decimated inside a window:
  30 s at 100 Hz is 3,000 points.
- **Fires and events** are never dropped.

Caching: responses are cached per (run, file hash, window snapped to ticks, layers, params) in
an in-memory LRU, and in Redis if `REDIS_URL` is set. Runs are immutable, so responses carry a
strong `ETag` and share pages may be cached by the browser. Unknown run or pipeline → `404`.
Bad parameters → `422`. A layer missing from the log is not an error: it is listed in
`missing`.

Performance target: repaint any window of a 15-minute run in ≤ 150 ms on the smallest Railway
plan. The client prefetches the next 2 s while playing.

Changes to this API need review. Additive changes keep `window_version`; anything else bumps it.

### Rest of the API (summary)

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness, DB and storage reachability (Railway health check). |
| `POST /api/runs` (multipart, chunked), `GET /api/runs`, `GET/DELETE /api/runs/{id}` | Upload, list, metadata, delete. Upload validates the log and builds the index. |
| `GET /api/compare?a={run}[:{pipeline}]&b=...` | Aligned clock and head-to-head stats. |
| `GET /api/runs/{id}/incidents`, `GET /api/runs/{id}/incidents/{n}/export` | Fire and event index; ±2 s JSON bundle for a safety report. |
| `GET/POST/DELETE /api/runs/{id}/annotations` | Annotations (`editor`). |
| `POST /api/runs/{id}/share`, `DELETE /api/share/{token}`, `GET /s/{token}` | Share links. |
| `POST /api/endpoints`, `WS /api/live/{endpoint_id}` (viewer), `WS /api/ingest/{endpoint_id}` (robot, bearer token) | Live telemetry fan-out. |
| `GET /api/bench/reports/...`, `POST /api/bench/verify` | Bench reports and leaderboard hash check. |
| `GET /api/library`, `GET /api/library/{clade}` | Species library, public. |
| `GET /api/profiles`, `GET /api/profiles/{name}/{version}` | Profile registry, spec and distillation report. Never prior values. |

---

## Head-to-head numbers

Each number on screen comes from one function in `stats.py`. Where `senseai-bench` defines a
metric, the formula and its unit-test vectors are the same.

| Tile | Definition |
|---|---|
| **First fire** | Time of the first tick at which `action` leaves `none`. A latched reflex is one fire. |
| **Lead time** | vs ground truth: `d = t_fire − t_gt`, the first fire attributed to the event (inside `[t_gt − pre, t_gt + post]`). Negative means the pipeline reacted before the actor entered the path. vs the other side: `lead(A over B) = d(B) − d(A)`. A miss is `d = +∞`, shown as "miss". |
| **Cells lit at t** | Number of `per_cell` cells above the cell threshold at the selected t, default 0.05 (the reflex's own path-cell cutoff). |
| **False fires** | Fires inside `clean_segments`. On a bench soak, also the rate per 100 h and the Garwood upper 95 % bound. |
| **Latency p50/p99** | Percentiles of `latency/e2e` over the run. |
| **Throughput proxy** | Mean of `speed_scale` over moving ticks. |

### The sample run

`python -m senseai_viewer.sample_data demo.h5` writes the synthetic run that the demo, the
screenshots and the tests use. It reproduces bench's `gate1_person_steps_out` scene. A robot
drives an aisle at 1.5 m/s under 0.5 lux, and a person steps out from behind a rack ahead-left.
The file holds one 12 s recording and two pipelines replayed on it, `panther` and `null` (the
default).

- **What the file contains.** It is 16 MB and takes about 2 s to generate. It holds 1,200
  ticks, about 300k events from a 346 × 260 event camera, 240 mono frames, a 32 × 32 surprise
  grid at every tick, 72 bearing bins, `event` and `frame` heads, reflex commands with
  explain sentences, cortex tracks and per-stage latency. The same seed always produces a
  byte-identical file.
- **How the fires arise.** Each fire comes from the stored surprise grid: path risk is
  `per_cell` weighted by the path corridor, and a fire is the first tick with risk above
  2.6. Nothing downstream is hard-coded, so `stats.py` computes the result rather than reading
  it back.
- **What the tiles show.**
  - Ground truth: the person becomes visible at 5.90 s and enters the path at 6.51 s.
  - First fire: Panther at 6.29 s (`d = −0.22 s`) and the default at 6.63 s (`d = +0.12 s`),
    so Panther leads by 0.34 s.
  - Cells lit at 6.29 s: Panther 45, the default 21.
  - False fires: Panther 0, the default 1 (at 2.99 s), caused by a lamp flicker around 3 s.
  - Latency e2e p99: about 11.2 ms for Panther and 10.8 ms for the default.

These numbers are **illustrative, not measured**. The file's `synthetic` attribute is true, and
the UI shows a "synthetic data" badge on every page that displays it. Real numbers come from
`senseai-bench` runs.

---

## Run it locally *(planned)*

```bash
uv sync --extra dev && (cd web && npm ci)
uv run python examples/demo_run.py --local      # builds the sample run, imports it, prints a URL
uv run senseai-viewer serve                      # http://127.0.0.1:8000, SQLite + ./data volume
```

## Deploy on Railway *(planned)*

1. Create a project from this repo and set the service root to `packages/senseai-viewer`.
   Railway builds the multi-stage `Dockerfile` (Node builds `web/`, then a slim Python runtime).
2. Add the Railway Postgres plugin. `DATABASE_URL` is injected, and Alembic migrations run
   on start.
3. Attach storage for log files: either a Railway volume mounted at `/data`, or an
   S3-compatible bucket. See the open question below.
4. Set `PUBLIC_BASE_URL`, `SESSION_SECRET`, `AUTH_*` and `MAX_UPLOAD_MB`, documented in
   `.env.example`.
5. Run `railway up`, then `python examples/demo_run.py --url $PUBLIC_BASE_URL`, which uploads
   the sample run and prints its public share link.

The health check is `/api/health`, configured in `railway.toml`.

## Interoperability guarantee

- The viewer consumes only the published schemas. A third-party pipeline that writes a RunLog
  with the layout above and the same protos appears in the viewer identically, with no code
  changes.
- `senseai-viewer validate <file>` checks a log against the layout and schemas. It reports
  which panels will light up and why any will not.
- The viewer imports no proprietary package at runtime and loads no weights. Nothing in it can
  reproduce a profile: the Profiles page shows specs and reports, never prior values.

## Privacy

- **Share links** expose one run read-only: its recording, pipelines and annotations, but not
  other runs, users or endpoints. They are rate-limited, revocable, and can expire.
- **Raw frames** from a live endpoint are off unless the endpoint opted in
  (`telemetry.raw_frames: true` in its SDK config), and the Live page shows a "raw frames off"
  indicator when they are off.
- **Uploaded runs** may contain camera frames. When uploading, the uploader can strip frames
  (`--no-frames`), which keeps events, surprise, reflex and cortex.

---

## Open questions for review

1. **Where the package lives.** The prompt assumes a monorepo (`packages/senseai-viewer/`).
   SenseAI is actually a set of repos, and this one already holds a shipped v0.1 at its root:
   a tick-file summary exporter with a uPlot SPA and HTTP Basic auth. I put the new package
   under `packages/senseai-viewer/`, as `senseai-bench` and `senseai-profiles` do, and left
   v0.1 untouched. The choice is between (a) replacing v0.1, folding its tick-file timeline
   into the new Replay page as a "recording only" view, and (b) keeping v0.1 as a separate
   tool.
2. **RunLog layout v0.** The bench, reflex and cortex contracts it stands in for are not
   written yet. It should be proposed to `senseai-bench` as its HDF5 layout, so that there is
   one layout rather than two.
3. **Storage.** The choice is between a Railway volume (cheapest and simplest, but tied to one
   service replica) and S3-compatible storage (needed for more than one replica, costs extra).
   Per the working rules I will stop and ask before choosing.
4. **Dependencies.** Server additions beyond the listed set are `msgpack` (window encoding)
   and `alembic` (migrations), both small. For the web bundle I intend to use no charting
   library over 1 MB: uPlot (about 50 kB) for the timeline and raw WebGL for the scene.
5. **The mockup.** The Playwright layout check needs the mockup image (1400 × 1000). I don't
   have it yet. Please add it to `docs/mockup.png`, or tell me where it is.

## Layout

```
packages/senseai-viewer/
  README.md                         this file
  server/senseai_viewer/
    sample_data.py                  synthetic Panther-vs-default run (done)
    app.py, settings.py, storage.py, stats.py, db/, readers/, api/   (next)
  web/                              Vite + React + TypeScript (next)
  tests/, examples/, Dockerfile, railway.toml, .env.example          (next)
```
