import { describe, expect, test } from "vitest";

import { splitBufferToBase64Chunks, joinBase64Chunks } from "../src/utils/upload-chunks.js";

describe("upload chunk helpers", () => {
  test("round-trips binary data through base64 chunks", () => {
    const source = Buffer.from("hello world ".repeat(1000), "utf8");
    const chunks = splitBufferToBase64Chunks(source, 256);
    expect(chunks.length).toBeGreaterThan(1);
    expect(joinBase64Chunks(chunks).equals(source)).toBe(true);
  });
});
