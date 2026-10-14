# Summary format (`senseai-viewer/summary`, version 1)

This document is the contract between the **export step** (`senseai-viewer export`) and the
**web app**. The exporter is the only thing that reads tick files. The web app reads only the
files described here. Anything not listed here is not part of the contract.

A summary is a directory with one recording in it. Locally it lives at `./out/<recording_id>/`.
In S3 it lives at `s3://$S3_BUCKET/$S3_PREFIX/<recording_id>/`, and `S3_PREFIX` defaults to `viewer`.

```
<recording_id>/
  manifest.json              required   what the recording is, its sources and its stats
  timeline.parquet           required   one row per tick: sample and event counts per source
  traces/<source_id>.parquet optional   downsampled numeric traces, one file per source that has one
  thumbs/index.json          optional   thumbnail → tick index, for every source with thumbnails
  thumbs/<source_id>/<k>.jpg optional   JPEG thumbnails, k = 0 … n−1
```

A summary of a 10 s, 4-source recording is typically 0.5–3 MB. Most of that is the thumbnails.

## Conventions

- **Time.** The tick-file contract defines tick `k` as the master-clock window
  `[t0_ns + k·tick_ns, t0_ns + (k+1)·tick_ns)`.
  Anything the UI plots uses **`t_rel_s`**: seconds since `t0_ns` at the *end* of the tick, so
  `t_rel_s = (k + 1) · tick_ns · 1e-9`.
  Absolute int64 nanosecond timestamps exceed 2⁵³, so they would lose precision as JSON numbers
  in a browser. They therefore appear:
  - as int64 columns in Parquet, which is exact;
  - as **decimal strings** in JSON, for example `"t0_ns": "1600000000000002000"`.

  Durations and periods such as `tick_ns` are small and stay JSON numbers.
- **Source ids** are the tick file's `source_ids`, unchanged. Each one must match
  `[A-Za-z0-9_.-]+`, because it becomes a file name. The exporter refuses a source id that doesn't.
- **Recording ids** match `[a-z0-9][a-z0-9._-]{0,127}`. By default the exporter derives the id from
  the tick file name: it lowercases the name and replaces every other character with `-`.
  You can set it with `--id`.
- **Missing numbers.** JSON has no NaN. A metric that the tick file reports as NaN is written as
  `null` in JSON (an example is jitter with no samples). In Parquet, a missing value is a null,
  not a NaN.
- **Determinism.** Exporting the same tick file twice, with the same exporter version, gives
  byte-identical output for every file except the `*.jpg` thumbnails. That includes
  `manifest.json`, `thumbs/index.json` and every `.parquet`.
  - The exporter achieves this with sorted JSON keys and fixed Parquet writer options. It never
    reads the wall clock, so `created_at` comes from the input file; see below.
  - In practice the JPEGs are identical too, but the contract doesn't promise it, because they
    depend on the Pillow/libjpeg build.
- **Upload order.** With `--upload`, the exporter uploads `manifest.json` **last**. A recording
  shows up in `GET /api/recordings` only after all its other files exist.

## `manifest.json`

UTF-8, `indent=2`, keys sorted. Example (abridged) for the synthetic DSEC fixture:

```json
{
  "created_at": "2026-10-14T21:03:11Z",
  "dataset": "dsec",
  "duration_s": 1.0,
  "exporter": { "name": "senseai-viewer", "version": "0.1.0", "senseai_ingest_version": "0.1.0" },
  "format": "senseai-viewer/summary",
  "n_ticks": 100,
  "recording_id": "zurich-city-04-a",
  "source_file": "zurich_city_04_a.h5",
  "sources": [
    {
      "modality": "event",
      "n_events": 200000,
      "n_samples": 1029,
      "nominal_rate_hz": 1000.0,
      "source_id": "events_left",
      "spec": {
        "clock_mode": "fixed", "drift_ppm": 0.0, "jitter_ns": 0,
        "meta": { "camera": "left", "event_rate_hz": 200000.4, "height": 480, "width": 640 },
        "offset_ns": "1600000000000000000", "offset_ref_ns": "0", "offset_samples": 0
      },
      "thumbs": 60,
      "trace": "traces/events_left.parquet"
    },
    {
      "modality": "frame",
      "n_events": 0,
      "n_samples": 20,
      "nominal_rate_hz": 20.0,
      "source_id": "rgb_left",
      "spec": { "clock_mode": "fixed", "meta": { "camera": "left", "encoding": "rgb8" }, "...": "..." },
      "thumbs": 20,
      "trace": null
    }
  ],
  "stats": {
    "sources": {
      "events_left": { "dropped": 0, "events": 200000, "high_water": 1, "late": 0,
                       "modality": "event", "samples": 1029, "samples_per_s": 1029.0 }
    },
    "staleness_p50_ns": 8000.0, "staleness_p99_ns": 5002000.0,
    "sync_jitter_p50_ns": 0.0, "sync_jitter_p99_ns": 0.0,
    "t0_ns": "1600000000000002000", "tick_ns": 10000000, "ticks": 100
  },
  "t0_ns": "1600000000000002000",
  "tick_ns": 10000000,
  "version": 1
}
```

| Field | Type | Meaning |
|---|---|---|
| `format` | string | Always `"senseai-viewer/summary"`. |
| `version` | int | Summary format version, currently `1`. See *Versioning*. |
| `recording_id` | string | Matches the directory name. |
| `dataset` | string | `config.dataset` from the tick file, such as `dsec`, `m3ed` or `wav`. It is `"unknown"` if the file wasn't written by `senseai-ingest replay`. |
| `source_file` | string | Base name of the tick file. The directory part is dropped. |
| `tick_ns` | int | Tick length in ns, from the tick file's attribute. |
| `n_ticks` | int | Number of ticks. Equals the number of rows in `timeline.parquet`. |
| `t0_ns` | string | Inclusive start of tick 0 in master-clock ns, as a decimal string. |
| `duration_s` | float | `n_ticks · tick_ns · 1e-9`. |
| `sources` | array | One entry per source, sorted by `source_id`, in the same order as the tick file. |
| `sources[].source_id` | string | |
| `sources[].modality` | string | A `senseai_ingest.stream.Modality` value: `event`, `frame`, `audio`, `imu`, `contact`, `current`, `thermal`, `gas`, `lidar` or `other`. |
| `sources[].nominal_rate_hz` | float | From the `SourceSpec`. |
| `sources[].n_samples` | int | Total samples. Equals the sum of `timeline.samples:<sid>`. |
| `sources[].n_events` | int | Total events for EVENT sources, `0` otherwise. |
| `sources[].spec` | object | The stored `SourceSpec`, minus the fields shown at the top level: `clock_mode`, `offset_ns`, `offset_ref_ns`, `drift_ppm`, `jitter_ns`, `offset_samples` and `meta`. ns offsets are strings. `meta` values that are arrays become nested lists. The `meta` keys **`file`, `dir`, `path` and `root` are removed**: they hold paths on the computer that ran the replay and are not useful in the viewer. |
| `sources[].trace` | string \| null | Relative path of the trace file, or `null` if this modality has none. |
| `sources[].thumbs` | int | Number of thumbnails, from 0 to 60. |
| `stats` | object \| null | The tick file's `stats` attribute, copied as-is, except that NaN becomes `null` and `t0_ns` becomes a string. It is `null` if the attribute is missing. Its shape is `Aligner.stats()` from `senseai-ingest`. The viewer reads `ticks`, `sync_jitter_p50_ns`, `sync_jitter_p99_ns`, `staleness_p50_ns`, `staleness_p99_ns`, and per source `samples_per_s`, `dropped` and `late`. It ignores other keys. |
| `created_at` | string | ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`). To keep exports deterministic, this is **the tick file's modification time**, or `$SOURCE_DATE_EPOCH` if that is set. It is never "now". |
| `exporter` | object | `name` and `version` of `senseai-viewer`, and the installed `senseai_ingest_version`. |

## `timeline.parquet`

One row per tick, in tick order, with exactly `n_ticks` rows.

| Column | Arrow type | Meaning |
|---|---|---|
| `tick` | int32 | Tick index, 0 … n_ticks−1. |
| `t_master_ns` | int64 | End of the tick window, exclusive, in master-clock ns. |
| `t_rel_s` | float64 | `(tick + 1) · tick_ns · 1e-9`. |
| `samples:<sid>` | uint32 | Samples of source `<sid>` in this tick. There is one column per source, in manifest order. |
| `events:<sid>` | uint32 | Events of source `<sid>` in this tick. This column exists **for EVENT sources only**. |

Writer settings are zstd compression, one row group and no dictionary encoding on the count
columns. At 10⁵ ticks with 4 sources the file stays below about 1 MB.

## `traces/<sid>.parquet`

These are downsampled numeric traces for the timeline lanes, **at most 5,000 rows**. There are
two kinds:

- **Bucketed** traces (EVENT, AUDIO and IMU) are aggregated over groups of consecutive ticks.
  - With `K = n_ticks` and `B = min(K, 5000)` buckets, bucket `b` covers the ticks
    `[⌊b·K/B⌋, ⌊(b+1)·K/B⌋)`.
  - When `K ≤ 5000`, each bucket is exactly one tick.
  - Every tick is in exactly one bucket, so each file has exactly `B` rows.
- **Sampled** traces (CURRENT, CONTACT and GAS) contain the raw sample values.
  - If a source has more than 5,000 samples, the exporter keeps samples `⌊i·N/5000⌋` for
    i = 0 … 4999, which is an even stride over sample order.
  - These sources are low-rate, so the stride rarely applies.

Every trace file starts with these columns:

| Column | Arrow type | Meaning |
|---|---|---|
| `tick` | int32 | Bucketed: the first tick of the bucket. Sampled: the tick the sample belongs to. |
| `t_rel_s` | float64 | Bucketed: end of the bucket's last tick, relative to `t0_ns`. Sampled: the sample's `t_master_ns − t0_ns`, in seconds. |

Bucketed traces also have `n_ticks` (int32), the number of ticks in the bucket.

The remaining columns depend on the modality:

| Modality | Kind | Columns | Definition |
|---|---|---|---|
| EVENT | bucketed | `events_per_tick` float64, `on_ratio` float64 (nullable) | Mean events per tick over the bucket, and `ON / (ON + OFF)` over all events in the bucket. `on_ratio` is null when the bucket has no events. |
| AUDIO | bucketed | `rms_0` … `rms_{C−1}` float32 (nullable) | RMS per channel over every audio frame of every block in the bucket. int16 audio is scaled by 1/32768, so all values are in [0, 1]. A value is null when the bucket has no audio. |
| IMU | bucketed | `ax`, `ay`, `az` (m/s²), `gx`, `gy`, `gz` (rad/s), float64 (nullable) | Mean of the samples in the bucket. Null when the bucket has none. |
| CURRENT | sampled | `j:<name>` float32 | One column per joint. The names come from `spec.meta.joints`, falling back to `j:0`, `j:1`, … |
| CONTACT | sampled | `force` kind: `f:0` … `f:{K−1}` float32. `bumper` kind: `mask` int64 | Raw values. |
| GAS | sampled | `g:<name>` float64 | One column per channel key, sorted. |
| FRAME, THERMAL, LIDAR, OTHER | — | — | No trace file. `sources[].trace` is `null`. Their timeline lane uses `timeline.parquet`. |

Each trace file's Arrow schema metadata has the key `senseai-viewer` with this JSON value:
`{"source_id": …, "modality": …, "kind": "bucketed" | "sampled", "buckets": B, "n_ticks": K}`.

## `thumbs/`

These are JPEG thumbnails at quality 80, baseline, with no EXIF data, **at most 320 px wide**,
with aspect ratio preserved. Images narrower than 320 px are not upscaled.

- **FRAME sources.**
  - When a source has `N` frames, the exporter picks `n = min(N, 60)` of them, at sample
    indices `⌊i·N/n⌋` for i = 0 … n−1.
  - It converts each frame by encoding:
    - `rgb8`: kept as RGB.
    - `mono8`: kept as grayscale.
    - `mono16` and other uint16 frames: shifted right by 8 bits.
    - `bayer_*`: shown as grayscale, without debayering.
    - `bgr8`: channels swapped to RGB.
    - Anything else: the first channel, shown as grayscale.
- **EVENT sources.**
  - The exporter picks up to 60 ticks at `⌊i·K/n⌋`, where `n = min(K, 60)`.
  - It accumulates each tick's events into a `width × height` image scaled to the thumbnail size.
    `width` and `height` come from the spec meta. If they are missing, the exporter uses the
    maximum x + 1 and y + 1 seen in the events.
  - The background is grey (128). A pixel whose last event was ON is white (255), and one whose
    last event was OFF is black (0).
  - The image is single-channel.
- Other modalities get no thumbnails.

### `thumbs/index.json`

This file lists every source that has at least one thumbnail. Keys are sorted, and
the file exists only if at least one source has thumbnails.

```json
{
  "events_left": [ { "k": 0, "tick": 0, "t_rel_s": 0.01 }, { "k": 1, "tick": 1, "t_rel_s": 0.02 } ],
  "rgb_left":    [ { "k": 0, "tick": 0, "t_rel_s": 0.01 }, { "k": 1, "tick": 5, "t_rel_s": 0.06 } ]
}
```

Entry `k` describes `thumbs/<sid>/<k>.jpg`.
- `tick` is the tick the frame or events belong to.
- Entries are in increasing `tick` order.
- To find the nearest thumbnail to a selected tick, the UI binary-searches `tick`.

## What the web app does when pieces are missing

The web app requires `manifest.json`. A recording directory without one is invisible.

| Missing | Effect in the UI |
|---|---|
| `timeline.parquet` | Timeline and scrubber show "not available". The stats card still renders. |
| `traces/<sid>.parquet` | That lane falls back to the sample counts from `timeline.parquet`, or "not available". |
| `thumbs/index.json` or a JPEG | The scrubber shows "not available" for that source. |
| `stats` is `null` or a stats key is missing | The card shows "—" for that value. |

## Versioning

`version` is an integer. The rules:

- **Additive** changes keep the version: a new optional manifest field, a new trace column, or a
  new file. Readers must ignore unknown fields and columns.
- **Breaking** changes increment it: removing or renaming a field, column or file, or changing
  a unit, type or meaning.
- The web app reads every version it knows, currently `{1}`. It lists a recording with an
  unknown version but greys it out with "summary version N not supported: re-export". To fix
  such a recording, re-run `senseai-viewer export`.

Any change to this format must update this document, `EXPORT_VERSION` in
`senseai_viewer/export/format.py`, and the golden test in `tests/test_export.py`.
