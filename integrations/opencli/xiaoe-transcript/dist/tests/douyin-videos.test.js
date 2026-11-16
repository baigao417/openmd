import { describe, expect, test } from "vitest";
import { extractSecUidFromDouyinUrl, normalizeDouyinBatchLimit } from "../src/providers/douyin-videos.js";
describe("extractSecUidFromDouyinUrl", () => {
    test("reads sec_uid from share urls and direct user urls", () => {
        expect(extractSecUidFromDouyinUrl("https://www.iesdouyin.com/share/user/demo?sec_uid=abc123")).toBe("abc123");
        expect(extractSecUidFromDouyinUrl("https://www.douyin.com/user/abc123")).toBe("abc123");
    });
    test("returns undefined for unsupported urls", () => {
        expect(extractSecUidFromDouyinUrl("https://example.com/user/abc")).toBeUndefined();
    });
});
describe("normalizeDouyinBatchLimit", () => {
    test("normalizes positive integer values and rejects invalid ones", () => {
        expect(normalizeDouyinBatchLimit(undefined)).toBeUndefined();
        expect(normalizeDouyinBatchLimit(12.8)).toBe(12);
        expect(normalizeDouyinBatchLimit(0)).toBeUndefined();
    });
});
