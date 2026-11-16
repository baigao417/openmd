import { describe, expect, test } from "vitest";
import { extractTranscriptTextFromWebPage, inferCreatorNameFromSubjectUrl, normalizeGetSubscriptionList, parseGetTopicTimestamp, pickTranscriptTextFromDetail } from "../src/providers/get-subscription.js";
describe("normalizeGetSubscriptionList", () => {
    test("maps topic manager payload into exportable subscription summaries", () => {
        const subscriptions = normalizeGetSubscriptionList({
            c: {
                list: [
                    {
                        id_alias: "vnd1y25Y",
                        name: "冤种小徐",
                        extend_data: {
                            stats_info: "1个内容 · 1人在用"
                        },
                        root_dir: {
                            id: 4085752
                        }
                    }
                ]
            }
        });
        expect(subscriptions).toEqual([
            expect.objectContaining({
                creatorId: "vnd1y25Y",
                creatorName: "冤种小徐",
                subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT",
                statsInfo: "1个内容 · 1人在用",
                rootDirectoryId: 4085752
            })
        ]);
    });
});
describe("pickTranscriptTextFromDetail", () => {
    test("prefers media text, then markdown summary, then cleaned summary, then dom fallback", () => {
        expect(pickTranscriptTextFromDetail({
            c: {
                post_media_text: "逐字稿正文",
                post_summary: "摘要",
                post_cleaned_summary: "清洗摘要"
            }
        }, "DOM fallback")).toEqual({
            content: "逐字稿正文",
            contentSource: "media_text"
        });
        expect(pickTranscriptTextFromDetail({
            c: {
                post_media_text: "",
                post_summary: "## 摘要",
                post_cleaned_summary: "清洗摘要"
            }
        }, "DOM fallback")).toEqual({
            content: "## 摘要",
            contentSource: "summary"
        });
        expect(pickTranscriptTextFromDetail({
            c: {
                post_media_text: "",
                post_summary: "",
                post_cleaned_summary: "第一行<br>第二行"
            }
        }, "DOM fallback")).toEqual({
            content: "第一行\n第二行",
            contentSource: "cleaned_summary"
        });
        expect(pickTranscriptTextFromDetail({
            c: {}
        }, "DOM fallback")).toEqual({
            content: "DOM fallback",
            contentSource: "dom"
        });
    });
});
describe("extractTranscriptTextFromWebPage", () => {
    test("prefers the clicked web transcript page content over the summary page", () => {
        const extracted = extractTranscriptTextFromWebPage({
            title: "怎么三小时内准备一场面试",
            sourceUrl: "https://www.iesdouyin.com/share/video/123",
            paragraphs: [
                "第一段正文",
                "第二段正文"
            ]
        });
        expect(extracted).toEqual({
            content: "第一段正文\n\n第二段正文",
            contentSource: "linked_web",
            sourceUrl: "https://www.iesdouyin.com/share/video/123"
        });
    });
});
describe("parseGetTopicTimestamp", () => {
    test("parses month-day timestamps using the current year and rolls back future dates", () => {
        const now = new Date("2026-01-03T08:00:00.000Z");
        expect(parseGetTopicTimestamp("01月02日 10:30", now)?.toISOString()).toBe("2026-01-02T10:30:00.000Z");
        expect(parseGetTopicTimestamp("12月31日 23:30", now)?.toISOString()).toBe("2025-12-31T23:30:00.000Z");
    });
});
describe("inferCreatorNameFromSubjectUrl", () => {
    test("uses followName from the exported Get subject url when present", () => {
        expect(inferCreatorNameFromSubjectUrl("https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442&followName=Cesare%E5%87%AF%E6%92%92%E8%87%AA%E7%84%B6%E5%81%A5%E8%BA%AB")).toBe("Cesare凯撒自然健身");
    });
});
