export function buildMarkdown(input) {
    const lines = [
        `# ${input.title}`,
        "",
        "## Transcript",
        ""
    ];
    for (const segment of input.segments) {
        lines.push(`[${segment.timestamp}] ${segment.text}`);
    }
    lines.push("");
    return lines.join("\n");
}
