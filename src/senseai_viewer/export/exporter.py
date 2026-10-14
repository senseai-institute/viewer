# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Read one tick file with :class:`~senseai_ingest.writer.TickReader` and write its summary.

The output layout and every column are specified in ``docs/summary-format.md``. The export is
deterministic: it never reads the wall clock, and all JSON and Parquet output is written with
fixed settings, so exporting the same file twice gives identical bytes.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from senseai_ingest.stream import Modality, Sample, SourceSpec
from senseai_ingest.writer import TickReader

from senseai_viewer.export.format import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    MAX_THUMBS,
    MAX_TRACE_POINTS,
    PRIVATE_META_KEYS,
    derive_recording_id,
    dumps_json,
    is_valid_recording_id,
    is_valid_source_id,
    to_jsonable,
)
from senseai_viewer.export.thumbs import encode_jpeg, evenly_spaced, events_to_image, frame_to_image

__all__ = ["ExportError", "ExportResult", "export_tick_file"]

Array = np.ndarray[Any, Any]
IMU_COLUMNS = ("ax", "ay", "az", "gx", "gy", "gz")


class ExportError(ValueError):
    """The tick file cannot be exported (bad recording id, unusable source id, ...)."""


@dataclass(frozen=True)
class ExportResult:
    """Outcome of :func:`export_tick_file`."""

    recording_id: str
    out_dir: Path
    manifest: dict[str, Any]
    files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Ctx:
    sid: str
    spec: SourceSpec
    n_ticks: int
    n_samples: int
    tick_ns: int
    t0_ns: int


# --------------------------------------------------------------------------- accumulators


class _Acc:
    """Collects what one source contributes to the summary during the single pass."""

    trace_kind: str | None = None

    def __init__(self, ctx: _Ctx) -> None:
        self.ctx = ctx
        self.n_events = 0
        self.thumbs: list[tuple[int, bytes]] = []  # (tick, jpeg), in tick order

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        """Consume the samples of tick ``k`` (only called for non-empty lists)."""

    def trace(self) -> pa.Table | None:
        """Return the trace table, or ``None`` if this modality has no trace."""
        return None


def _bucket_bounds(n_ticks: int) -> tuple[Array, Array]:
    b = min(n_ticks, MAX_TRACE_POINTS)
    i = np.arange(b + 1, dtype=np.int64)
    edges = (i * n_ticks) // b if b else np.zeros(1, np.int64)
    return edges[:-1], edges[1:]


def _bucket_sum(per_tick: Array, starts: Array) -> Array:
    if len(starts) == 0:
        return per_tick[:0]
    out: Array = np.add.reduceat(per_tick, starts, axis=0)
    return out


def _nullable(values: Array, type_: pa.DataType) -> pa.Array:
    return pa.array(values, type=type_, from_pandas=True)


class _Bucketed(_Acc):
    trace_kind = "bucketed"

    def _table(self, columns: Mapping[str, pa.Array]) -> pa.Table:
        starts, ends = _bucket_bounds(self.ctx.n_ticks)
        base = {
            "tick": pa.array(starts.astype(np.int32)),
            "t_rel_s": pa.array(ends * self.ctx.tick_ns / 1e9, pa.float64()),
            "n_ticks": pa.array((ends - starts).astype(np.int32)),
        }
        return pa.table({**base, **columns})

    def bounds(self) -> tuple[Array, Array]:
        return _bucket_bounds(self.ctx.n_ticks)


class _EventAcc(_Bucketed):
    def __init__(self, ctx: _Ctx) -> None:
        super().__init__(ctx)
        self.events = np.zeros(ctx.n_ticks, np.int64)
        self.on = np.zeros(ctx.n_ticks, np.int64)
        self.thumb_ticks = set(evenly_spaced(ctx.n_ticks, MAX_THUMBS))
        meta = ctx.spec.meta
        self.width = int(meta["width"]) if meta.get("width") else None
        self.height = int(meta["height"]) if meta.get("height") else None

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        for s in samples:
            p = s.columns["p"]
            self.events[k] += len(p)
            self.on[k] += int(np.count_nonzero(p))

    def add_thumb(self, k: int, samples: Sequence[Sample]) -> None:
        cols: dict[str, Array] = {}
        for key in ("x", "y", "p"):
            parts = [s.columns[key] for s in samples]
            cols[key] = np.concatenate(parts) if parts else np.zeros(0)
        self.thumbs.append((k, encode_jpeg(events_to_image(cols, self.width, self.height))))

    def trace(self) -> pa.Table:
        starts, ends = self.bounds()
        ev = _bucket_sum(self.events, starts).astype(np.float64)
        on = _bucket_sum(self.on, starts).astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(ev > 0, on / ev, np.nan)
        return self._table(
            {
                "events_per_tick": pa.array(ev / np.maximum(ends - starts, 1), pa.float64()),
                "on_ratio": _nullable(ratio, pa.float64()),
            }
        )


class _FrameAcc(_Acc):
    def __init__(self, ctx: _Ctx) -> None:
        super().__init__(ctx)
        self.selected = set(evenly_spaced(ctx.n_samples, MAX_THUMBS))
        self.encoding = ctx.spec.meta.get("encoding")
        self.seen = 0

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        for s in samples:
            if self.seen in self.selected:
                img = frame_to_image(s.array, str(self.encoding) if self.encoding else None)
                self.thumbs.append((k, encode_jpeg(img)))
            self.seen += 1


class _AudioAcc(_Bucketed):
    def __init__(self, ctx: _Ctx) -> None:
        super().__init__(ctx)
        self.sumsq: Array | None = None
        self.frames = np.zeros(ctx.n_ticks, np.int64)

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        for s in samples:
            a = s.array
            if a.ndim == 1:
                a = a[:, None]
            x = a.astype(np.float64)
            if a.dtype == np.int16:
                x /= 32768.0
            if self.sumsq is None:
                self.sumsq = np.zeros((self.ctx.n_ticks, a.shape[1]), np.float64)
            c = min(a.shape[1], self.sumsq.shape[1])
            self.sumsq[k, :c] += np.square(x[:, :c]).sum(axis=0)
            self.frames[k] += len(a)

    def trace(self) -> pa.Table:
        starts, _ = self.bounds()
        cols: dict[str, pa.Array] = {}
        if self.sumsq is not None:
            n = _bucket_sum(self.frames, starts).astype(np.float64)
            ss = _bucket_sum(self.sumsq, starts)
            with np.errstate(invalid="ignore", divide="ignore"):
                rms = np.where(n[:, None] > 0, np.sqrt(ss / n[:, None]), np.nan)
            for c in range(rms.shape[1]):
                cols[f"rms_{c}"] = _nullable(rms[:, c].astype(np.float32), pa.float32())
        return self._table(cols)


class _ImuAcc(_Bucketed):
    def __init__(self, ctx: _Ctx) -> None:
        super().__init__(ctx)
        self.sums = np.zeros((ctx.n_ticks, 6), np.float64)
        self.count = np.zeros(ctx.n_ticks, np.int64)

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        for s in samples:
            v = np.asarray(s.array, np.float64).reshape(-1)[:6]
            self.sums[k, : len(v)] += v
            self.count[k] += 1

    def trace(self) -> pa.Table:
        starts, _ = self.bounds()
        n = _bucket_sum(self.count, starts).astype(np.float64)
        s = _bucket_sum(self.sums, starts)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(n[:, None] > 0, s / n[:, None], np.nan)
        return self._table(
            {name: _nullable(mean[:, i], pa.float64()) for i, name in enumerate(IMU_COLUMNS)}
        )


class _SampledAcc(_Acc):
    """CURRENT / CONTACT / GAS: raw sample values, stride-decimated to at most 5,000 rows."""

    trace_kind = "sampled"

    def __init__(self, ctx: _Ctx) -> None:
        super().__init__(ctx)
        self.selected = set(evenly_spaced(ctx.n_samples, MAX_TRACE_POINTS))
        self.seen = 0
        self.ticks: list[int] = []
        self.t_rel: list[float] = []
        self.rows: list[Sample] = []

    def add(self, k: int, samples: Sequence[Sample]) -> None:
        for s in samples:
            if self.seen in self.selected:
                self.ticks.append(k)
                self.t_rel.append((s.t_master_ns - self.ctx.t0_ns) / 1e9)
                self.rows.append(s)
            self.seen += 1

    def _values(self) -> dict[str, pa.Array]:
        raise NotImplementedError

    def trace(self) -> pa.Table:
        base = {
            "tick": pa.array(np.asarray(self.ticks, np.int32)),
            "t_rel_s": pa.array(np.asarray(self.t_rel, np.float64)),
        }
        return pa.table({**base, **self._values()})

    def _matrix(self) -> Array:
        vecs = [np.asarray(s.array, np.float64).reshape(-1) for s in self.rows]
        width = max((len(v) for v in vecs), default=0)
        m = np.full((len(vecs), width), np.nan)
        for i, v in enumerate(vecs):
            m[i, : len(v)] = v
        return m


class _CurrentAcc(_SampledAcc):
    def _values(self) -> dict[str, pa.Array]:
        m = self._matrix()
        joints = self.ctx.spec.meta.get("joints")
        names = (
            [str(j) for j in joints]
            if isinstance(joints, list | tuple) and len(joints) == m.shape[1]
            else [str(i) for i in range(m.shape[1])]
        )
        return {
            f"j:{n}": _nullable(m[:, i].astype(np.float32), pa.float32())
            for i, n in enumerate(names)
        }


class _ContactAcc(_SampledAcc):
    def _values(self) -> dict[str, pa.Array]:
        kind = self.ctx.spec.meta.get("kind")
        integer = bool(self.rows) and np.issubdtype(self.rows[0].array.dtype, np.integer)
        if kind == "bumper" or (kind is None and integer):
            masks = [int(np.asarray(s.array).reshape(-1)[0]) for s in self.rows]
            return {"mask": pa.array(masks, pa.int64())}
        m = self._matrix()
        return {
            f"f:{i}": _nullable(m[:, i].astype(np.float32), pa.float32()) for i in range(m.shape[1])
        }


class _GasAcc(_SampledAcc):
    def _values(self) -> dict[str, pa.Array]:
        names = sorted({k for s in self.rows for k in s.columns})
        out: dict[str, pa.Array] = {}
        for name in names:
            vals = []
            for s in self.rows:
                v = np.asarray(s.columns.get(name, np.zeros(0))).reshape(-1)
                vals.append(float(v[0]) if v.size else np.nan)
            out[f"g:{name}"] = _nullable(np.asarray(vals, np.float64), pa.float64())
        return out


_ACCS: dict[Modality, type[_Acc]] = {
    Modality.EVENT: _EventAcc,
    Modality.FRAME: _FrameAcc,
    Modality.AUDIO: _AudioAcc,
    Modality.IMU: _ImuAcc,
    Modality.CURRENT: _CurrentAcc,
    Modality.CONTACT: _ContactAcc,
    Modality.GAS: _GasAcc,
}


# --------------------------------------------------------------------------- manifest helpers


def _spec_json(spec: SourceSpec) -> dict[str, Any]:
    meta = {k: v for k, v in spec.meta.items() if k not in PRIVATE_META_KEYS}
    return {
        "clock_mode": spec.clock_mode.value,
        "drift_ppm": float(spec.drift_ppm),
        "jitter_ns": int(spec.jitter_ns),
        "meta": to_jsonable(meta),
        "offset_ns": str(int(spec.offset_ns)),
        "offset_ref_ns": str(int(spec.offset_ref_ns)),
        "offset_samples": int(spec.offset_samples),
    }


def _created_at(path: Path) -> str:
    sde = os.environ.get("SOURCE_DATE_EPOCH")
    ts = int(sde) if sde else int(path.stat().st_mtime)
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - both packages are installed in practice
        return "unknown"


def _stats(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = to_jsonable(raw)
    if isinstance(out.get("t0_ns"), int):
        out["t0_ns"] = str(out["t0_ns"])
    return out


def _write_parquet(table: pa.Table, path: Path, meta: Mapping[str, Any] | None = None) -> None:
    if meta is not None:
        blob = json.dumps(to_jsonable(meta), sort_keys=True, separators=(",", ":"))
        table = table.replace_schema_metadata({"senseai-viewer": blob})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd", use_dictionary=False, write_statistics=True)


# --------------------------------------------------------------------------- entry point


def export_tick_file(
    tick_file: str | Path,
    out_root: str | Path = "out",
    recording_id: str | None = None,
) -> ExportResult:
    """Export ``tick_file`` to ``<out_root>/<recording_id>/`` and return what was written.

    The directory is written to a temporary sibling first and then swapped in, so a previous
    export of the same id is replaced atomically and stale files never linger.
    """
    path = Path(tick_file)
    rid = recording_id if recording_id is not None else derive_recording_id(path.name)
    if not is_valid_recording_id(rid):
        raise ExportError(f"invalid recording id {rid!r}: use [a-z0-9][a-z0-9._-]{{0,127}}")
    root = Path(out_root)
    tmp = root / f".{rid}.partial"
    final = root / rid
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        manifest = _export_into(path, tmp, rid)
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    files = sorted(p.relative_to(final).as_posix() for p in final.rglob("*") if p.is_file())
    return ExportResult(rid, final, manifest, files)


def _export_into(path: Path, out: Path, rid: str) -> dict[str, Any]:
    with TickReader(path) as reader:
        attrs = reader.attrs()
        specs = reader.specs()
        sids = list(reader.source_ids)
        for sid in sids:
            if not is_valid_source_id(sid):
                raise ExportError(f"source id {sid!r} cannot be used as a file name")
        n_ticks = len(reader)
        tick_ns = reader.tick_ns
        t0_ns = reader.t0_ns
        counts = np.asarray(reader.counts, np.int64).reshape(n_ticks, len(sids))
        accs: dict[str, _Acc] = {}
        for j, sid in enumerate(sids):
            spec = specs[sid]
            ctx = _Ctx(sid, spec, n_ticks, int(counts[:, j].sum()), tick_ns, t0_ns)
            accs[sid] = _ACCS.get(spec.modality, _Acc)(ctx)
        event_thumbs = {
            sid: acc.thumb_ticks for sid, acc in accs.items() if isinstance(acc, _EventAcc)
        }
        for tick in reader:
            for sid, samples in tick.samples.items():
                acc = accs[sid]
                if samples:
                    acc.add(tick.index, samples)
                if sid in event_thumbs and tick.index in event_thumbs[sid]:
                    assert isinstance(acc, _EventAcc)
                    acc.add_thumb(tick.index, samples)
        t_master = np.asarray(reader.t_master_ns, np.int64)

    # timeline.parquet
    ticks = np.arange(n_ticks, dtype=np.int64)
    cols: dict[str, pa.Array] = {
        "tick": pa.array(ticks.astype(np.int32)),
        "t_master_ns": pa.array(t_master, pa.int64()),
        "t_rel_s": pa.array((ticks + 1) * tick_ns / 1e9, pa.float64()),
    }
    for j, sid in enumerate(sids):
        cols[f"samples:{sid}"] = pa.array(counts[:, j].astype(np.uint32))
    for sid, acc in accs.items():
        if isinstance(acc, _EventAcc):
            cols[f"events:{sid}"] = pa.array(acc.events.astype(np.uint32))
            acc.n_events = int(acc.events.sum())
    _write_parquet(pa.table(cols), out / "timeline.parquet")

    # traces/ and thumbs/
    thumbs_index: dict[str, list[dict[str, Any]]] = {}
    sources: list[dict[str, Any]] = []
    for j, sid in enumerate(sids):
        acc = accs[sid]
        spec = specs[sid]
        table = acc.trace()
        trace_rel = None
        if table is not None:
            trace_rel = f"traces/{sid}.parquet"
            meta = {
                "source_id": sid,
                "modality": spec.modality.value,
                "kind": acc.trace_kind,
                "buckets": table.num_rows,
                "n_ticks": n_ticks,
            }
            _write_parquet(table, out / trace_rel, meta)
        if acc.thumbs:
            d = out / "thumbs" / sid
            d.mkdir(parents=True, exist_ok=True)
            entries = []
            for k, (tick_i, jpeg) in enumerate(acc.thumbs):
                (d / f"{k}.jpg").write_bytes(jpeg)
                entries.append({"k": k, "tick": tick_i, "t_rel_s": (tick_i + 1) * tick_ns / 1e9})
            thumbs_index[sid] = entries
        sources.append(
            {
                "source_id": sid,
                "modality": spec.modality.value,
                "nominal_rate_hz": float(spec.nominal_rate_hz),
                "n_samples": int(counts[:, j].sum()),
                "n_events": acc.n_events,
                "spec": _spec_json(spec),
                "trace": trace_rel,
                "thumbs": len(acc.thumbs),
            }
        )
    if thumbs_index:
        (out / "thumbs" / "index.json").write_bytes(dumps_json(thumbs_index))

    config = attrs.get("config")
    dataset = config.get("dataset") if isinstance(config, Mapping) else None
    manifest: dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "recording_id": rid,
        "dataset": str(dataset) if dataset else "unknown",
        "source_file": path.name,
        "tick_ns": tick_ns,
        "n_ticks": n_ticks,
        "t0_ns": str(t0_ns),
        "duration_s": n_ticks * tick_ns / 1e9,
        "sources": sources,
        "stats": _stats(attrs.get("stats")),
        "created_at": _created_at(path),
        "exporter": {
            "name": "senseai-viewer",
            "version": _pkg_version("senseai-viewer"),
            "senseai_ingest_version": _pkg_version("senseai-ingest"),
        },
    }
    (out / "manifest.json").write_bytes(dumps_json(manifest))
    return manifest
