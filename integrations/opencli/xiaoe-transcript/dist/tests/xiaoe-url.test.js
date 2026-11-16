import { describe, expect, test } from "vitest";
import { detectXiaoePage } from "../src/app/xiaoe-page.js";
describe("detectXiaoePage", () => {
    test("recognizes XiaoeTong video pages", () => {
        const result = detectXiaoePage({
            url: "https://appttezzxyn5858.h5.xet.citv.cn/p/course/video/v_123",
            title: "课程标题"
        });
        expect(result.supported).toBe(true);
        expect(result.reason).toBeUndefined();
    });
    test("recognizes XiaoeTong alive replay pages on xiaoecloud", () => {
        const result = detectXiaoePage({
            url: "https://appm1dwjnky5483.h5.xiaoecloud.com/v4/course/alive/l_69d1f094e4b0694c34f4b044?app_id=appm1dwjnky5483&type=2",
            title: "直播回放课程"
        });
        expect(result.supported).toBe(true);
        expect(result.reason).toBeUndefined();
    });
    test("recognizes XiaoeTong alive replay pages on pomoho", () => {
        const result = detectXiaoePage({
            url: "https://appm1dwjnky5483.h5.xet.pomoho.com/v2/course/alive/l_68baf9cce4b0694c5b264616?type=2",
            title: "直播回放课程"
        });
        expect(result.supported).toBe(true);
        expect(result.reason).toBeUndefined();
    });
    test("recognizes supported replay URLs when query hints are present", () => {
        const result = detectXiaoePage({
            url: "https://example.com/v4/course/alive/l_69d1f094e4b0694c34f4b044?product_id=p_123&app_id=appm1dwjnky5483",
            title: "带参数的直播回放"
        });
        expect(result.supported).toBe(true);
        expect(result.reason).toBeUndefined();
    });
    test("rejects non video pages", () => {
        const result = detectXiaoePage({
            url: "https://example.com/",
            title: "Example"
        });
        expect(result.supported).toBe(false);
        expect(result.reason).toContain("当前页面不是");
    });
});
