import { describe, expect, test } from "vitest";
import { chooseBestMediaCandidate, collectMediaUrlsDeep, collectXiaoeAliveLookbackUrls } from "../src/pipeline/xiaoe-media.js";
describe("collectMediaUrlsDeep", () => {
    test("finds media urls inside nested JSON-like structures", () => {
        const results = collectMediaUrlsDeep({
            data: {
                playlist: {
                    audio: "https://cdn.example.com/audio-track.m4a",
                    stream: {
                        master: "https://cdn.example.com/master.m3u8"
                    }
                }
            }
        });
        expect(results).toEqual([
            "https://cdn.example.com/audio-track.m4a",
            "https://cdn.example.com/master.m3u8"
        ]);
    });
    test("decodes html entities in embedded media urls", () => {
        const results = collectMediaUrlsDeep("https://cdn.example.com/master.m3u8?sign=abc&amp;t=123&amp;us=xyz");
        expect(results).toEqual([
            "https://cdn.example.com/master.m3u8?sign=abc&t=123&us=xyz"
        ]);
    });
});
describe("collectXiaoeAliveLookbackUrls", () => {
    test("prefers the default lookback line over a confusion_index performance URL", () => {
        const results = collectXiaoeAliveLookbackUrls({
            code: 0,
            data: [
                {
                    line_name: "线路1",
                    default: false,
                    line_sharpness: [
                        {
                            name: "原画",
                            default: true,
                            url: "https://ttv-tos.xet.tech/bucket/material/playlist_eof.m3u8?sign=normal-a"
                        }
                    ]
                },
                {
                    line_name: "线路3",
                    default: true,
                    line_sharpness: [
                        {
                            name: "原画",
                            default: true,
                            url: "https://v-tos-k.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=normal-default"
                        }
                    ]
                }
            ]
        });
        expect(results).toEqual([
            "https://v-tos-k.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=normal-default",
            "https://ttv-tos.xet.tech/bucket/material/playlist_eof.m3u8?sign=normal-a"
        ]);
    });
    test("ignores malformed lookback entries", () => {
        const results = collectXiaoeAliveLookbackUrls({
            data: [
                { line_sharpness: [{ url: "" }, { url: "https://example.com/not-media" }] },
                { line_sharpness: [{ url: "https://cdn.example.com/audio.m3u8?x=1&amp;y=2" }] }
            ]
        });
        expect(results).toEqual(["https://cdn.example.com/audio.m3u8?x=1&y=2"]);
    });
});
describe("chooseBestMediaCandidate", () => {
    test("prefers audio or lower-resolution HLS candidates for transcript downloads", () => {
        const selectedAudio = chooseBestMediaCandidate([
            "https://cdn.example.com/video/playlist_eof.m3u8?resolution=1920x1080",
            "https://cdn.example.com/audio.m4a?sign=ok"
        ]);
        expect(selectedAudio).toBe("https://cdn.example.com/audio.m4a?sign=ok");
        const selectedLowResolution = chooseBestMediaCandidate([
            "https://cdn.example.com/video/playlist_eof.m3u8?resolution=1920x1080",
            "https://cdn.example.com/video/playlist_eof.m3u8?resolution=640x360"
        ]);
        expect(selectedLowResolution).toBe("https://cdn.example.com/video/playlist_eof.m3u8?resolution=640x360");
    });
    test("prefers faster Xiaoe CDN mirrors over the default encrypted VOD host", () => {
        const selected = chooseBestMediaCandidate([
            "https://encrypt-k-vod.xet.tech/bucket/material/playlist_eof.m3u8?sign=default",
            "https://c-vod.hw-cdn.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=mirror"
        ]);
        expect(selected).toBe("https://c-vod.hw-cdn.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=mirror");
    });
    test("does not select Xiaoe confusion_index playlists", () => {
        const selected = chooseBestMediaCandidate([
            "https://pri-cdn-tx.xiaoeknow.com/app/confusion_index/bad.m3u8?sign=redacted",
            "https://v-tos-k.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=normal"
        ]);
        expect(selected).toBe("https://v-tos-k.xiaoeknow.com/bucket/material/playlist_eof.m3u8?sign=normal");
    });
    test("returns empty instead of known unusable Xiaoe confusion_index playlist", () => {
        const selected = chooseBestMediaCandidate([
            "https://pri-cdn-tx.xiaoeknow.com/app/confusion_index/bad.m3u8?sign=redacted"
        ]);
        expect(selected).toBeUndefined();
    });
});
