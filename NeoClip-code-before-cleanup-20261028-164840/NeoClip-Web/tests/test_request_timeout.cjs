const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const start = html.indexOf('    async function api(');
const context = {AbortController, setTimeout, clearTimeout};
vm.createContext(context);
vm.runInContext(html.slice(start, html.indexOf('\n    function ', start)), context);
(async () => {
    context.fetch = (_url, options) => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
    await assert.rejects(context.api('/api/files?details=true', {timeoutMs:20}), /too long/);
    // The deadline covers a stuck response body as well as the initial fetch.
    context.fetch = async (_url, options) => ({ok:true, json:() => new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    })});
    await assert.rejects(context.api('/api/files?details=true', {timeoutMs:20}), /too long/);
    context.fetch = async () => ({ok:true, json:async () => ({files:[]})});
    assert.equal((await context.api('/api/files?details=true', {timeoutMs:20})).files.length, 0);
    console.log('Request timeout and recovery checks passed');
})().catch(error => {console.error(error);process.exitCode=1});
