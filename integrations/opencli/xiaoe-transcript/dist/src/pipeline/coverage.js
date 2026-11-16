const RAW_TEXT_COMPLETE_CHARS_PER_SECOND = 2;
export function computeCoverage(transcriptDurationSec, mediaDurationSec) {
    if (mediaDurationSec <= 0) {
        return {
            coverage: 0,
            isComplete: false
        };
    }
    const coverage = Number((transcriptDurationSec / mediaDurationSec).toFixed(4));
    return {
        coverage,
        isComplete: coverage >= 0.85
    };
}
export function computeCoverageWithRawText(transcriptDurationSec, mediaDurationSec, rawText) {
    const timestamp = computeCoverage(transcriptDurationSec, mediaDurationSec);
    const normalizedRawText = (rawText || "").replace(/\s+/g, "").trim();
    const rawTextCoverage = mediaDurationSec > 0 && normalizedRawText
        ? Number(Math.min(1, normalizedRawText.length / (mediaDurationSec * RAW_TEXT_COMPLETE_CHARS_PER_SECOND)).toFixed(4))
        : 0;
    const coverage = Math.max(timestamp.coverage, rawTextCoverage);
    return {
        coverage,
        isComplete: coverage >= 0.85,
        timestampCoverage: timestamp.coverage,
        rawTextCoverage
    };
}
