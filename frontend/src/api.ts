// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
// Typed access to the viewer's JSON API (see README "API" and docs/summary-format.md).

export type Cell = number | string | null;

export interface Columnar {
  n_rows: number;
  columns: Record<string, Cell[]>;
  meta: { kind?: string; modality?: string; source_id?: string } | null;
}

export interface RecordingRow {
  id: string;
  dataset: string | null;
  source_file: string | null;
  duration_s: number | null;
  n_ticks: number | null;
  n_sources: number;
  modalities: string[];
  jitter_p50_ns: number | null;
  jitter_p99_ns: number | null;
  dropped: number | null;
  late: number | null;
  created_at: string | null;
  version: number | null;
  supported: boolean;
}

export interface SourceStats {
  modality?: string;
  samples?: number;
  samples_per_s?: number | null;
  events?: number;
  dropped?: number;
  late?: number;
  high_water?: number;
}

export interface SourceEntry {
  source_id: string;
  modality: string;
  nominal_rate_hz: number;
  n_samples: number;
  n_events: number;
  spec: { clock_mode?: string; meta?: Record<string, unknown> };
  trace: string | null;
  thumbs: number;
}

export interface Manifest {
  recording_id: string;
  dataset: string;
  source_file: string;
  version: number;
  supported: boolean;
  tick_ns: number;
  n_ticks: number;
  t0_ns: string;
  duration_s: number;
  sources: SourceEntry[];
  created_at: string;
  stats: {
    ticks?: number;
    sync_jitter_p50_ns?: number | null;
    sync_jitter_p99_ns?: number | null;
    staleness_p50_ns?: number | null;
    staleness_p99_ns?: number | null;
    sources?: Record<string, SourceStats>;
  } | null;
}

export interface ThumbEntry {
  k: number;
  tick: number;
  t_rel_s: number;
}
export type ThumbIndex = Record<string, ThumbEntry[]>;

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** GET a JSON endpoint. Resolves to `null` on 404 ("not available"), throws otherwise. */
export async function getJSON<T>(path: string): Promise<T | null> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (r.status === 404) return null;
  if (!r.ok) throw new ApiError(r.status, `${r.status} ${r.statusText} for ${path}`);
  return (await r.json()) as T;
}

const enc = encodeURIComponent;

export const api = {
  recordings: async () =>
    (await getJSON<{ recordings: RecordingRow[] }>("/api/recordings"))?.recordings ?? [],
  manifest: (id: string) => getJSON<Manifest>(`/api/recordings/${enc(id)}`),
  timeline: (id: string) => getJSON<Columnar>(`/api/recordings/${enc(id)}/timeline`),
  trace: (id: string, sid: string) =>
    getJSON<Columnar>(`/api/recordings/${enc(id)}/traces/${enc(sid)}`),
  thumbs: (id: string) => getJSON<ThumbIndex>(`/api/recordings/${enc(id)}/thumbs/index.json`),
  thumbUrl: (id: string, sid: string, k: number) =>
    `/api/recordings/${enc(id)}/thumbs/${enc(sid)}/${k}.jpg`,
};

/** Column as numbers (strings/nulls become null). */
export function nums(t: Columnar | null, name: string): (number | null)[] | null {
  const col = t?.columns[name];
  if (!col) return null;
  return col.map((v) => (typeof v === "number" ? v : null));
}

/** X-axis column as plain numbers (x values are never null in a valid summary). */
export function xnums(t: Columnar | null, name = "t_rel_s"): number[] | null {
  const col = t?.columns[name];
  if (!col) return null;
  return col.map((v) => (typeof v === "number" ? v : Number.NaN));
}
