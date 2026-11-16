import { describe, expect, test } from "vitest";

import {
  extractDouyinAwemeIdFromUrl,
  normalizeDouyinCommentLimit
} from "../src/providers/douyin-comments.js";

describe("extractDouyinAwemeIdFromUrl", () => {
  test("parses aweme ids from direct douyin video urls", () => {
    expect(extractDouyinAwemeIdFromUrl("https://www.douyin.com/video/7627047128858328650")).toBe("7627047128858328650");
    expect(extractDouyinAwemeIdFromUrl("https://www.iesdouyin.com/share/video/7627047128858328650/")).toBe("7627047128858328650");
  });

  test("falls back to modal_id query params when present", () => {
    expect(extractDouyinAwemeIdFromUrl("https://www.douyin.com/user/demo?modal_id=7627047128858328650")).toBe("7627047128858328650");
  });

  test("returns undefined for unsupported or empty urls", () => {
    expect(extractDouyinAwemeIdFromUrl("https://example.com/video/1")).toBeUndefined();
    expect(extractDouyinAwemeIdFromUrl("")).toBeUndefined();
  });
});

describe("normalizeDouyinCommentLimit", () => {
  test("clamps values into the supported range", () => {
    expect(normalizeDouyinCommentLimit(undefined)).toBe(10);
    expect(normalizeDouyinCommentLimit(0)).toBe(1);
    expect(normalizeDouyinCommentLimit(99)).toBe(20);
    expect(normalizeDouyinCommentLimit(6)).toBe(6);
  });
});
