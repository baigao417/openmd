import { describe, expect, test } from "vitest";
import { parseGetEventStream, summarizeGetStream } from "../src/providers/get-parser.js";
describe("parseGetEventStream", () => {
    test("extracts structured events from SSE text", () => {
        const events = parseGetEventStream([
            "id:0",
            'data: {"code":200,"msg_type":-1,"data":{"note_id":"123"}}',
            "",
            "id:1",
            'data: {"code":200,"msg_type":-7,"data":{"msg":"{\\"task_status\\":1000,\\"sentence_list\\":[{\\"seek\\":12,\\"text\\":\\"hello\\"}]}"}}',
            "",
            "id:2",
            'data: {"code":200,"msg_type":1,"data":{"msg":"{\\"content\\":\\"A\\"}"}}',
            "",
            "id:3",
            'data: {"code":200,"msg_type":1,"data":{"msg":"{\\"content\\":\\"B\\"}"}}',
            "",
            "id:4",
            'data: {"code":200,"msg_type":101,"data":{"msg":"stop"}}'
        ].join("\n"));
        expect(events).toHaveLength(5);
        expect(events[0]?.msgType).toBe(-1);
        expect(events[1]?.msgType).toBe(-7);
        expect(events[4]?.msgType).toBe(101);
    });
});
describe("summarizeGetStream", () => {
    test("builds note id, sentence list, and streamed summary", () => {
        const summary = summarizeGetStream(parseGetEventStream([
            "id:0",
            'data: {"code":200,"msg_type":-1,"data":{"note_id":"123"}}',
            "",
            "id:1",
            'data: {"code":200,"msg_type":-6,"data":{"msg":"{\\"section_list\\":[{\\"title\\":\\"intro\\",\\"seek\\":0}]}"}}',
            "",
            "id:2",
            'data: {"code":200,"msg_type":-7,"data":{"msg":"{\\"task_status\\":1000,\\"sentence_list\\":[{\\"seek\\":12,\\"text\\":\\"hello\\"}]}"}}',
            "",
            "id:3",
            'data: {"code":200,"msg_type":1,"data":{"msg":"{\\"content\\":\\"A\\"}"}}',
            "",
            "id:4",
            'data: {"code":200,"msg_type":1,"data":{"msg":"{\\"content\\":\\"B\\"}"}}',
            "",
            "id:5",
            'data: {"code":200,"msg_type":101,"data":{"msg":"stop"}}'
        ].join("\n")));
        expect(summary.noteId).toBe("123");
        expect(summary.done).toBe(true);
        expect(summary.summaryText).toBe("AB");
        expect(summary.sections).toEqual([{ title: "intro", seek: 0 }]);
        expect(summary.sentences).toEqual([{ seek: 12, text: "hello" }]);
    });
});
