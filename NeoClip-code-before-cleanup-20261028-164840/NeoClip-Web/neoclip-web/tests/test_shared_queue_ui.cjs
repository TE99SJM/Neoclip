const {readFileSync} = require('node:fs');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const list = {children: [], set innerHTML(v) { this.children = []; }, appendChild(v) {this.children.push(v);}};
let removed;
const ctx = {
    document: {getElementById: () => list, createElement: () => ({button: {}, querySelector() {return this.button;}})},
    escapeHtml: s => s, formatTime: s => s, removeClip: (...args) => {removed=args;}
};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('    let renderedQueueSignature'), html.indexOf('    async function startRender()')), ctx);
const clip = {id: 'stable-id', name:'Shared clip', start_s:1,end_s:2};
ctx.renderClipQueue({source_path:'a.mp4',clips:[clip]});
const first = list.children[0];
ctx.renderClipQueue({source_path:'a.mp4',clips:[clip]});
assert.equal(list.children[0], first, 'unchanged polling does not rebuild controls');
first.button.onclick();
assert.deepEqual(removed, [0, 'stable-id']);
ctx.renderClipQueue({source_path:'a.mp4',clips:[clip,{...clip,id:'second'}]});
assert.equal(list.children.length,2, 'other app additions appear');
ctx.renderClipQueue({source_path:'b.mp4',clips:[]});
assert.equal(list.children.length,0);
console.log('Shared queue refresh keeps unchanged controls and removes clips by stable ID');
