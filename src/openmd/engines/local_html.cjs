// Parse supplied HTML only. No browser, scripts, page fetch or resource loads.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const denyNetwork = () => { throw new Error('local_html_network_disabled'); };
globalThis.fetch = denyNetwork;
for (const protocol of ['node:http', 'node:https']) {
  const api = require(protocol);
  api.request = denyNetwork;
  api.get = denyNetwork;
}
const [modulePath, source] = process.argv.slice(2);
const { parseLinkedomHTML } = require(path.join(path.dirname(modulePath), 'utils/linkedom-compat.js'));
const { Defuddle } = require(modulePath);
const doc = parseLinkedomHTML(fs.readFileSync(source, 'utf8'), 'about:blank');
for (const el of doc.querySelectorAll('script,iframe,object,embed,link,base,meta[http-equiv]')) el.remove();
Defuddle(doc, 'about:blank', { markdown: true, removeExactSelectors: false })
  .then(result => process.stdout.write(JSON.stringify(result)))
  .catch(() => { process.stderr.write('local_html_parse_failed'); process.exitCode = 1; });
