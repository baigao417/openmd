import { describe, expect, test, vi } from "vitest";
vi.mock("../src/opencli/client.js", () => ({
    fetchDaemonStatus: vi.fn().mockResolvedValue({ extensionConnected: true })
}));
vi.mock("../src/utils/command.js", () => ({
    redactSensitiveText: (value) => value,
    runCommand: vi.fn().mockResolvedValue({ exitCode: 0, stdout: "", stderr: "" })
}));
import { assertOpenCliEnvironment } from "../src/workflow.js";
import { runCommand } from "../src/utils/command.js";
describe("assertOpenCliEnvironment", () => {
    test("uses the supported opencli doctor command", async () => {
        await assertOpenCliEnvironment();
        expect(runCommand).toHaveBeenCalledWith("opencli", ["doctor"], {
            allowFailure: true,
            timeoutMs: 20000
        });
    });
});
