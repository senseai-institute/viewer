// Copyright 2026 SenseAI Institute
// SPDX-License-Identifier: Apache-2.0
//
// Decoder for the msgpack subset the server writes (server/senseai_viewer/wire.py), plus
// conversion of {dtype, shape, data} arrays into typed arrays.

export type Typed =
  | Uint8Array
  | Int8Array
  | Uint16Array
  | Int16Array
  | Uint32Array
  | Int32Array
  | Float32Array
  | Float64Array;

export interface NDArray<T extends Typed = Typed> {
  dtype: string;
  shape: number[];
  data: T;
}

export function decode(buf: ArrayBuffer | Uint8Array): unknown {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const text = new TextDecoder();
  let pos = 0;
  const u32 = () => {
    const v = view.getUint32(pos);
    pos += 4;
    return v;
  };
  const read = (): unknown => {
    const t = bytes[pos++];
    if (t < 0x80) return t;
    if (t >= 0xe0) return t - 0x100;
    if (t >= 0xa0 && t <= 0xbf) {
      const n = t & 0x1f;
      const s = text.decode(bytes.subarray(pos, pos + n));
      pos += n;
      return s;
    }
    switch (t) {
      case 0xc0:
        return null;
      case 0xc2:
        return false;
      case 0xc3:
        return true;
      case 0xce:
        return u32();
      case 0xcf: {
        const v = Number(view.getBigUint64(pos));
        pos += 8;
        return v;
      }
      case 0xd2: {
        const v = view.getInt32(pos);
        pos += 4;
        return v;
      }
      case 0xd3: {
        const v = Number(view.getBigInt64(pos));
        pos += 8;
        return v;
      }
      case 0xcb: {
        const v = view.getFloat64(pos);
        pos += 8;
        return v;
      }
      case 0xdb: {
        const n = u32();
        const s = text.decode(bytes.subarray(pos, pos + n));
        pos += n;
        return s;
      }
      case 0xc6: {
        const n = u32();
        const b = bytes.slice(pos, pos + n); // copy: aligned buffer for typed views
        pos += n;
        return b;
      }
      case 0xdd: {
        const n = u32();
        const out = new Array<unknown>(n);
        for (let i = 0; i < n; i++) out[i] = read();
        return out;
      }
      case 0xdf: {
        const n = u32();
        const out: Record<string, unknown> = {};
        for (let i = 0; i < n; i++) {
          const k = read() as string;
          out[k] = read();
        }
        return toArray(out);
      }
      default:
        throw new Error(`msgpack: unsupported type 0x${t.toString(16)}`);
    }
  };
  return read();
}

/** Convert a decoded {dtype, shape, data} map to an NDArray with a typed view. */
function toArray(o: Record<string, unknown>): unknown {
  const keys = Object.keys(o);
  if (keys.length !== 3 || !(o.data instanceof Uint8Array) || typeof o.dtype !== "string") return o;
  const b = o.data as Uint8Array;
  const shape = o.shape as number[];
  const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;
  let data: Typed;
  switch (o.dtype) {
    case "u1":
    case "b1":
      data = new Uint8Array(ab);
      break;
    case "i1":
      data = new Int8Array(ab);
      break;
    case "u2":
      data = new Uint16Array(ab);
      break;
    case "i2":
      data = new Int16Array(ab);
      break;
    case "u4":
      data = new Uint32Array(ab);
      break;
    case "i4":
      data = new Int32Array(ab);
      break;
    case "f2":
      data = f16ToF32(new Uint16Array(ab));
      break;
    case "f4":
      data = new Float32Array(ab);
      break;
    case "f8":
      data = new Float64Array(ab);
      break;
    default:
      throw new Error(`msgpack: unsupported dtype ${o.dtype}`);
  }
  return { dtype: o.dtype, shape, data } satisfies NDArray;
}

/** IEEE half → float32 (little-endian input already in host order). */
export function f16ToF32(h: Uint16Array): Float32Array {
  const out = new Float32Array(h.length);
  for (let i = 0; i < h.length; i++) {
    const x = h[i];
    const s = x & 0x8000 ? -1 : 1;
    const e = (x >> 10) & 0x1f;
    const m = x & 0x3ff;
    if (e === 0) out[i] = s * m * 2 ** -24;
    else if (e === 31) out[i] = m ? NaN : s * Infinity;
    else out[i] = s * (1 + m / 1024) * 2 ** (e - 15);
  }
  return out;
}
