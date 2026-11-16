import { readFile } from "node:fs/promises";
import { describe, expect, test } from "vitest";

describe("opencli extension popup", () => {
  async function loadXiaoeCatalogUrlDetector(): Promise<(url: string) => boolean> {
    const js = await readFile("opencli-extension/popup.js", "utf8");
    const start = js.indexOf("function isKnownXiaoeHost");
    const end = js.indexOf("function hasXiaoeQueryHints");
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    return new Function(`${js.slice(start, end)}; return isXiaoeCourseCatalogUrl;`)() as (url: string) => boolean;
  }

  test("keeps xiaoe batch operations inside the popup", async () => {
    const html = await readFile("opencli-extension/popup.html", "utf8");
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(html).toContain("批量转写（直接在扩展内选择）");
    expect(html).toContain("batch-card");
    expect(js).toContain("/catalog");
    expect(js).toContain("/batch-run");
    expect(js).not.toContain("window.open(`${HELPER_CONFIGS.xiaoe.baseUrl}/batch`)");
  });

  test("sends helper token headers for mutating helper requests", async () => {
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(js).toContain("helperToken");
    expect(js).toContain("X-OpenCLI-Helper-Token");
    expect(js).toContain("security?.token");
  });

  test("explains disabled actions and exposes explicit popup batch controls", async () => {
    const html = await readFile("opencli-extension/popup.html", "utf8");
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(html).toContain("id=\"run-current-reason\"");
    expect(html).toContain("id=\"batch-action-reason\"");
    expect(html).toContain("id=\"batch-auto-load\"");
    expect(js).toContain("getRunCurrentReason");
    expect(js).toContain("getBatchActionReason");
    expect(js).toContain("shouldSelectBatchItemByDefault");
    expect(js).toContain("confirmLargeBatchRun");
    expect(js).toContain("共 ${counts.total} 条");
  });

  test("does not auto-load batch catalog from a single xiaoe media page", async () => {
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(js).toContain("function isXiaoeCourseCatalogUrl");
    expect(js).toContain("isXiaoeCourseCatalogUrl(tabInfo.url)");
    expect(js).not.toContain("isLikelyXiaoeUrl(tabInfo.url) && !input.value.trim()");
    expect(js).toContain("hasCatalogUrl");
    expect(js).toContain("!hasCatalogUrl");
  });

  test("allows xiaoe course and column catalog urls but rejects single media pages", async () => {
    const isXiaoeCourseCatalogUrl = await loadXiaoeCatalogUrlDetector();

    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xiaoecloud.com/p/course/course_123")).toBe(true);
    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xet.citv.cn/p/course/ecourse/course_123")).toBe(true);
    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xet.pomoho.com/p/course/ecourse/course_123")).toBe(true);
    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xet.citv.cn/v1/course/column/p_111?type=3")).toBe(true);
    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xiaoecloud.com/v4/course/alive/l_123?type=2")).toBe(false);
    expect(isXiaoeCourseCatalogUrl("https://appabc.h5.xiaoecloud.com/p/course/video/v_123")).toBe(false);
    expect(isXiaoeCourseCatalogUrl("https://example.com/p/course/course_123")).toBe(false);
  });

  test("uses the supported opencli doctor command in popup hints", async () => {
    const html = await readFile("opencli-extension/popup.html", "utf8");
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(html).toContain("opencli doctor");
    expect(js).toContain('opencli doctor"');
    expect(html).not.toContain("--no-live");
    expect(js).not.toContain("--no-live");
  });

  test("persists the batch auto-load checkbox preference", async () => {
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(js).toContain("BATCH_AUTO_LOAD_STORAGE_KEY");
    expect(js).toContain("restoreBatchAutoLoadPreference");
    expect(js).toContain("persistBatchAutoLoadPreference");
  });

  test("restores catalog results after the popup is closed during a background read", async () => {
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(js).toContain("/catalog-status");
    expect(js).toContain("restoreBatchCatalogFromHelper");
    expect(js).toContain("applyCatalogStatus");
    expect(js).toContain("catalog: 后台读取中");
  });

  test("does not clear selected catalog rows during periodic status restore", async () => {
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(js).toContain("function batchItemsMatch");
    expect(js).toContain("shouldPreserveSelection");
    expect(js).toContain("previousSelectedIds.has(itemId)");
    expect(js).toContain("!hasRenderedItems || !batchItemsMatch(POPUP_STATE.batchItems, nextItems)");
  });

  test("exposes a popup stop button for cancelling the running helper task", async () => {
    const html = await readFile("opencli-extension/popup.html", "utf8");
    const js = await readFile("opencli-extension/popup.js", "utf8");

    expect(html).toContain("id=\"stop-current-task\"");
    expect(html).toContain("停止当前任务");
    expect(js).toContain("stopCurrentTask");
    expect(js).toContain('"/shutdown"');
    expect(js).toContain("stop-current-task");
    expect(js).toContain("window.confirm");
  });
});
