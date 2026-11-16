import { beforeEach, expect, test, vi } from "vitest";
vi.mock("../src/utils/command.js", () => ({ runCommand: vi.fn() }));
import { runCommand } from "../src/utils/command.js";
import { verifySilentAudioTail } from "../src/providers/audio-tail.js";
beforeEach(() => { vi.mocked(runCommand).mockReset(); });
test("verifies the observed short silent suffix without changing the timestamp", async () => {
    vi.mocked(runCommand).mockResolvedValue({ stdout: "", stderr: "silence_start: 0\nsilence_end: 0.1055", exitCode: 0 });
    expect(await verifySilentAudioTail("audio.mp3", 4.91, 5.0155)).toBe(true);
    expect(vi.mocked(runCommand).mock.calls[0][1]).toContain("4.91");
});
test.each(["", "silence_start: 0.05\nsilence_end: 0.1055", "silence_start: 0\nsilence_end: 0.05",
    "silence_start: 0\nsilence_end: 0.04\nsilence_start: 0.06\nsilence_end: 0.1055"])("rejects a suffix with unproven or audible content: %s", async (stderr) => {
    vi.mocked(runCommand).mockResolvedValue({ stdout: "", stderr, exitCode: 0 });
    expect(await verifySilentAudioTail("audio.mp3", 4.91, 5.0155)).toBe(false);
});
test.each([[4, 5], [0.8, 1], [NaN, 5], [0, 5], [5, 5]])("rejects large or invalid gaps %s/%s without running ffmpeg", async (end, duration) => {
    expect(await verifySilentAudioTail("audio.mp3", end, duration)).toBe(false);
    expect(runCommand).not.toHaveBeenCalled();
});
test("fails closed when the acoustic check fails", async () => {
    vi.mocked(runCommand).mockRejectedValue(new Error("decoder failed"));
    expect(await verifySilentAudioTail("audio.mp3", 4.91, 5.0155)).toBe(false);
});
