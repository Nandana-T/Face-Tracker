import type { FrameHeader, StreamMessage } from "./api-types";

const MAX_HEADER_BYTES = 32 * 1024;

export class ProtocolError extends Error {}

/**
 * Binary stream message: `[uint32 big-endian N][N bytes UTF-8 JSON header][JPEG bytes]`
 * (see backend app/workers/protocol.py).
 */
export function unpackFrame(buffer: ArrayBuffer): { header: FrameHeader; jpeg: Uint8Array } {
  if (buffer.byteLength < 4) throw new ProtocolError("frame too short");
  const view = new DataView(buffer);
  const n = view.getUint32(0, false);
  if (n > MAX_HEADER_BYTES || 4 + n > buffer.byteLength) throw new ProtocolError("bad header length");
  let header: FrameHeader;
  try {
    header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, n))) as FrameHeader;
  } catch {
    throw new ProtocolError("bad header");
  }
  if (
    typeof header?.frame_width !== "number" ||
    typeof header?.frame_height !== "number" ||
    !Array.isArray(header?.faces) ||
    (header.tracks !== undefined && !Array.isArray(header.tracks))
  ) {
    throw new ProtocolError("header missing fields");
  }
  const jpeg = new Uint8Array(buffer, 4 + n);
  if (jpeg.length < 2 || jpeg[0] !== 0xff || jpeg[1] !== 0xd8) throw new ProtocolError("not a JPEG");
  return { header, jpeg };
}

export function parseControl(text: string): StreamMessage | null {
  try {
    const data = JSON.parse(text) as StreamMessage;
    return typeof data?.type === "string" ? data : null;
  } catch {
    return null;
  }
}
