import { runCommand } from "../utils/command.js";
/** Accept only a measured, very short silent tail; never inflate word timestamps. */
export async function verifySilentAudioTail(audioPath, lastEndSec, audioDurationSec) {
    const gap = audioDurationSec - lastEndSec;
    if (!Number.isFinite(gap) || lastEndSec <= 0 || gap <= 0 || gap > 0.25 || gap / audioDurationSec > 0.05)
        return false;
    try {
        const result = await runCommand("ffmpeg", ["-hide_banner", "-nostdin", "-ss", String(lastEndSec),
            "-i", audioPath, "-t", String(gap), "-af", "silencedetect=noise=-40dB:d=0.03", "-f", "null", "-"], { timeoutMs: 30000 });
        const events = [...result.stderr.matchAll(/silence_(start|end):\s*([\d.]+)/g)];
        // Require one uninterrupted interval covering the entire decoded suffix.
        return events.length === 2 && events[0][1] === "start" && Number(events[0][2]) === 0
            && events[1][1] === "end" && Number(events[1][2]) >= gap - 0.002;
    }
    catch {
        return false;
    }
}
