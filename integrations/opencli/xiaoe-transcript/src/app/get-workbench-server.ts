import { createGetWorkbenchServer } from "./get-workbench-service.js";

const port = Number.parseInt(process.env.GET_WORKBENCH_PORT ?? "19829", 10);
const server = await createGetWorkbenchServer({ port });

console.log(`[get-workbench] listening on ${server.baseUrl}`);

process.on("SIGINT", async () => {
  await server.close();
  process.exit(0);
});

process.on("SIGTERM", async () => {
  await server.close();
  process.exit(0);
});
