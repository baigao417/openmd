export class WorkflowStageError extends Error {
  readonly stage: string;
  readonly tempDir?: string;

  constructor(stage: string, cause: unknown, tempDir?: string) {
    super(formatStageError(stage, cause, tempDir));
    this.name = "WorkflowStageError";
    this.stage = stage;
    this.tempDir = tempDir;
  }
}

export function formatStageError(stage: string, error: unknown, tempDir?: string): string {
  const message = error instanceof Error ? error.message : String(error);
  if (tempDir) {
    return `failed at ${stage}: ${message} (temp dir: ${tempDir})`;
  }
  return `failed at ${stage}: ${message}`;
}
