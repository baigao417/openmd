import { formatTimestamp } from "../utils/time.js";
export function mergeTranscriptParts(title, parts) {
    const chapters = parts.flatMap((part) => part.result.chapters.map((chapter) => ({
        ...chapter,
        startSec: chapter.startSec + part.offsetSec,
        timestamp: formatTimestamp(chapter.startSec + part.offsetSec)
    })));
    const segments = parts.flatMap((part) => part.result.segments.map((segment) => ({
        ...segment,
        startSec: segment.startSec + part.offsetSec,
        timestamp: formatTimestamp(segment.startSec + part.offsetSec)
    })));
    const transcriptDurationSec = parts.reduce((maxDuration, part) => {
        return Math.max(maxDuration, part.offsetSec + part.result.transcriptDurationSec);
    }, 0);
    const rawText = parts
        .map((part, index) => `## Part ${index + 1}\n\n${part.result.rawText || ''}`.trim())
        .join("\n\n");
    return {
        provider: "get",
        title,
        transcriptDurationSec,
        chapters,
        segments,
        rawText
    };
}
