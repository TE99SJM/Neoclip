const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const gestureCode = html.slice(html.indexOf('    let editorRefreshInFlight'), html.indexOf('    function applyEditorSession'));
const saveCode = html.slice(html.indexOf('    async function saveEditorSessionNow('), html.indexOf('    function scheduleEditorSessionSave'));

function setup(save = async () => {}) {
    const events = {}, elements = new Map(), requests = [];
    let reloads = 0, beacons = 0;
    const payload = {path:'folder/movie.mp4', current_time:123, mark_in:100, mark_out:125, clip_name:'Keep me'};
    const ctx = {
        document: {
            documentElement: {scrollTop:0}, body: {scrollTop:0}, fullscreenElement:null,
            getElementById(id) {
                if (!html.includes(`id="${id}"`)) return null;
                if (!elements.has(id)) elements.set(id, {textContent:'', disabled:false,
                    classList: {toggle() {}}});
                return elements.get(id);
            },
            addEventListener(name, fn, options) {
                events[name] = fn;
                if (name === 'touchmove') assert.equal(options.passive, false);
            }
        },
        window: {scrollY:0, location:{reload() {reloads++}}, getComputedStyle:node => ({overflowY:node.overflowY || 'visible'})},
        sessionSaveTimer:null, lastSavedSessionJson:'', clearTimeout() {},
        editorSessionPayload:() => payload,
        api:async (url, options) => {requests.push({url, options});return save()},
        saveEditorSessionBeacon() {beacons++},
    };
    vm.createContext(ctx);
    vm.runInContext(saveCode + gestureCode, ctx);
    const target = {closest:() => null, parentElement:ctx.document.body};
    const touch = (x,y) => ({clientX:x,clientY:y});
    const start = (options={}) => events.touchstart({target, touches:[touch(20,20)], ...options});
    const move = (x,y) => {
        let prevented = false;
        events.touchmove({touches:[touch(x,y)], cancelable:true, preventDefault(){prevented=true}});
        return prevented;
    };
    return {ctx, events, elements, requests, payload, target, start, move,
        get reloads(){return reloads}, get beacons(){return beacons}};
}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
(async()=>{
    assert.match(html, /\.version\s*\{[^}]*touch-action:\s*pan-x/);
    assert.doesNotMatch(html, /id="playerRefreshButton"|id="pullRefreshHandle"/);
    const s=setup();
    s.start();s.move(20,60);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'short pull does not refresh');
    s.start();s.move(20,120);s.move(20,35);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'moving back above threshold cancels refresh');
    s.start();s.move(120,30);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'horizontal drag does not refresh');
    s.start();s.move(20,5);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'normal upward swipe does not refresh');
    s.ctx.window.scrollY=100;s.start();assert.equal(s.move(20,150),false);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'scrolling within page does not refresh');s.ctx.window.scrollY=0;
    s.start({touches:[{},{}]});s.move(20,150);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'multi-touch is ignored');
    s.start({target:{closest:()=>({})}});assert.equal(s.move(20,150),false);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'interactive controls retain their gestures');
    s.start({target:{closest:()=>null,scrollHeight:100,clientHeight:30,overflowY:'auto',parentElement:s.ctx.document.body}});
    s.move(20,150);s.events.touchend();await flush();assert.equal(s.reloads,0,'nested scrolling panel is ignored');
    s.ctx.document.fullscreenElement={};s.start();s.move(20,150);s.events.touchend();await flush();
    assert.equal(s.reloads,0,'fullscreen player is ignored');s.ctx.document.fullscreenElement=null;
    s.start();s.move(20,150);s.events.touchcancel();s.events.touchend();await flush();
    assert.equal(s.reloads,0,'cancelled gesture does not refresh');
    let finish;
    const held=setup(()=>new Promise(resolve=>{finish=resolve}));
    held.start();assert.equal(held.move(20,120),true);
    assert.equal(held.elements.get('pullRefreshStatus').textContent,'Release to refresh');
    held.events.touchend();await flush();
    assert.equal(held.reloads,0,'wait for session save before reload');
    assert.equal(held.requests.length,1);
    assert.equal(held.requests[0].url,'/api/session');
    assert.equal(held.requests[0].options.timeoutMs,2500,'session save is bounded');
    assert.deepEqual(JSON.parse(held.requests[0].options.body),held.payload,'preserve position, marks and name');
    await held.ctx.refreshEditor();assert.equal(held.requests.length,1,'duplicate refresh ignored');
    finish();await flush();assert.equal(held.reloads,1);
    const failed=setup(async()=>{throw new Error('Server timeout')});
    await failed.ctx.refreshEditor();assert.equal(failed.beacons,1);assert.equal(failed.reloads,1,'server failure still reloads');
    const manual=setup();await manual.ctx.refreshEditor();assert.equal(manual.reloads,1);
    assert.match(html, /id="refreshButton"[^>]*onclick="refreshEditor\(\)"/);
    console.log('Pull-down gestures, cancellation, controls, saved session, timeout fallback and manual refresh passed');
})().catch(error=>{console.error(error);process.exitCode=1});

