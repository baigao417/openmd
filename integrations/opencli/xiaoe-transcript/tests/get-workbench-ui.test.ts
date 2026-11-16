import { afterEach, describe, expect, test } from "vitest";

import { createGetWorkbenchServer } from "../src/app/get-workbench-service.js";

const closers: Array<() => Promise<void>> = [];

afterEach(async () => {
  while (closers.length > 0) {
    await closers.pop()?.();
  }
});

describe("get workbench ui shell", () => {
  test("serves the workbench HTML shell and static assets", async () => {
    const server = await createGetWorkbenchServer({
      port: 0,
      getHelperStatus: async () => null,
      listSubscriptions: async () => [],
      runExport: async () => ({ accepted: true }),
      startHelper: async () => ({ started: true }),
      stopHelper: async () => ({ stopped: true })
    });
    closers.push(async () => server.close());

    const html = await fetch(`${server.baseUrl}/`).then((res) => res.text());
    const css = await fetch(`${server.baseUrl}/styles.css`).then((res) => res.text());
    const js = await fetch(`${server.baseUrl}/app.js`).then((res) => res.text());

    expect(html).toContain("Get 导出工作台");
    expect(html).toContain("/app.js");
    expect(css).toContain(".log-drawer-panel");
    expect(js).toContain("data-log-drawer-trigger");
  });

  test("includes overlay log drawer markers in the shell", async () => {
    const server = await createGetWorkbenchServer({
      port: 0,
      getHelperStatus: async () => null,
      listSubscriptions: async () => [],
      runExport: async () => ({ accepted: true }),
      startHelper: async () => ({ started: true }),
      stopHelper: async () => ({ stopped: true })
    });
    closers.push(async () => server.close());

    const js = await fetch(`${server.baseUrl}/app.js`).then((res) => res.text());

    expect(js).toContain("data-log-drawer");
    expect(js).toContain("data-log-drawer-close");
  });

  test("documents that empty dates export all content and preserves filter interaction hooks", async () => {
    const server = await createGetWorkbenchServer({
      port: 0,
      getHelperStatus: async () => null,
      listSubscriptions: async () => [],
      runExport: async () => ({ accepted: true }),
      startHelper: async () => ({ started: true }),
      stopHelper: async () => ({ stopped: true })
    });
    closers.push(async () => server.close());

    const js = await fetch(`${server.baseUrl}/app.js`).then((res) => res.text());

    expect(js).toContain("留空则导出全部");
    expect(js).toContain("focusin");
    expect(js).toContain("filterInteracting");
  });
});
