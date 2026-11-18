// Execute only Openmd-owned adapters through OpenCLI's actual dispatcher.
// Never load the user's plugins, adapters, credentials or update notifier.
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const [host, command, url] = process.argv.slice(2);
if (!['play-url', 'catalog', 'content'].includes(command)) throw new Error('Unsupported command');
const moduleUrl = file => pathToFileURL(resolve(host, 'dist/src', file)).href;
registerHooks({resolve(specifier, context, nextResolve) {
  const files = {'@jackwener/opencli/registry': 'registry-api.js', '@jackwener/opencli/errors': 'errors.js'};
  if (files[specifier]) return {url: moduleUrl(files[specifier]), shortCircuit: true};
  return nextResolve(specifier, context);
}});
try {
  await import(new URL(`./${command}.js`, import.meta.url));
  const {getRegistry} = await import(moduleUrl('registry.js'));
  const {executeCommand} = await import(moduleUrl('execution.js'));
  const cmd = getRegistry().get(`xiaoe/${command}`);
  if (!cmd) throw new Error('adapter_missing');
  const result = await executeCommand(cmd, {url}, false, {windowMode: 'background', siteSession: 'ephemeral', keepTab: 'false'});
  process.stdout.write(JSON.stringify(result) + '\n');
} catch (error) {
  // Browser errors may carry signed locators/session details. Expose only type.
  process.stderr.write(JSON.stringify({error: {name: error?.name || 'Error', message: 'Xiaoe adapter execution failed'}}) + '\n');
  process.exitCode = 1;
}
