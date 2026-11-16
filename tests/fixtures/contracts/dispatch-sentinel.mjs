// Offline replay against the actual installed OpenCLI dispatcher ABI.
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
const host = process.argv[2];
const load = file => import(pathToFileURL(resolve(host, 'dist/src', file)).href);
const {executeCommand} = await load('execution.js');
const marker = 'sentinel 中文 & space';
const command = {site:'offline', name:'sentinel', access:'read', browser:false,
  args:[{name:'value', required:true, type:'string'}, {name:'count',type:'int'}],
  func: async args => [{value:args.value, count:args.count}]};
const rows = await executeCommand(command, {value:marker,count:'7'});
assert.equal(rows[0].value, marker);
assert.equal(rows[0].count, 7);
await assert.rejects(executeCommand(command, {value:marker,count:'bad'}), /number/);
await assert.rejects(executeCommand(command, {count:1}), /required/);
process.stdout.write(JSON.stringify({ok:true,dispatcher:true,sentinel:true,typed_error:true})+'\n');
