export class WorkflowStageError extends Error {
    stage;
    tempDir;
    constructor(stage, cause, tempDir) {
        super(formatStageError(stage, cause, tempDir));
        this.name = "WorkflowStageError";
        this.stage = stage;
        this.tempDir = tempDir;
    }
}
export function formatStageError(stage, error, tempDir) {
    const message = error instanceof Error ? error.message : String(error);
    if (tempDir) {
        return `failed at ${stage}: ${message} (temp dir: ${tempDir})`;
    }
    return `failed at ${stage}: ${message}`;
}
