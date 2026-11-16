import type { MarkdownInput } from "../types.js";

export function buildMarkdown(input: MarkdownInput): string {
  const lines: string[] = [
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
