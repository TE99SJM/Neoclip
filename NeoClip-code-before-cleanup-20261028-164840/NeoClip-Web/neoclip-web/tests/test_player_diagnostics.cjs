const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const script = readFileSync(`${__dirname}/../app/web/player-diagnostics.js`, 'utf8');
const events = {}, docEvents = {}, windowEvents = {}, timers = [], requests = [];
const video = {addEventListener(name, fn) { events[name] = fn; }};
for (const [key,value] of Object.entries({currentTime:6,paused:false,readyState:4,networkState:2,
    playbackRate:1,videoWidth:426,videoHeight:240,error:null})) {
    Object.defineProperty(video,key,{get:()=>value,set:()=>{throw Error(`Diagnostics modified ${key}`)}});
}
for (const method of ['play','pause','load']) video[method]=()=>{throw Error(`Diagnostics called ${method}`)};
let trackChanged;
video.videoTracks={length:1,addEventListener(name,fn){if(name==='change')trackChanged=fn}};
let resize, mutation, fail = false;
const ctx = {window:{addEventListener:(name,fn)=>windowEvents[name]=fn},
    document:{visibilityState:'visible',addEventListener:(name,fn)=>docEvents[name]=fn},
    AbortController, setTimeout:()=>1,clearTimeout(){},setInterval:fn=>timers.push(fn),
    fetch:async(url, options)=>{if(fail)throw Error('offline');requests.push(JSON.parse(options.body));return {ok:true}},
    ResizeObserver: class {constructor(fn){resize=fn}observe(){}},
    MutationObserver: class {constructor(fn){mutation=fn}observe(){}},
};
vm.createContext(ctx);
vm.runInContext(script,ctx);
ctx.window.installNeoClipPlayerDiagnostics(video,()=> 'proxy');
(async()=>{
    events.loadedmetadata();trackChanged();
    resize([{contentRect:{width:320,height:180}}]);
    resize([{contentRect:{width:320,height:180}}]);
    mutation([{attributeName:'src'}]);
    await timers[0]();
    const logged=requests[0].events;
    assert.equal(logged.filter(e=>e.event==='layout-resize').length,1);
    for(const name of ['loadedmetadata','videoTracks:change','attribute:src']) assert.ok(logged.some(e=>e.event===name));
    assert.ok(logged.every(e=>e.media_time===6 && e.paused===false && e.proxy_id==='proxy'));
    const sent=requests.length;
    await timers[0]();assert.equal(requests.length,sent,'empty timer does not send requests');
    for(let i=0;i<200;i++)events.loadedmetadata();
    await timers[0]();await timers[0]();await timers[0]();await timers[0]();
    assert.equal(requests.length,sent+3,'client queue is capped at 96 events');
    assert.ok(requests.every(r=>r.events.length<=32));
    fail=true;events.error();await timers[0]();
    fail=false;events.playing();await timers[0]();
    assert.equal(requests.at(-1).events[0].event,'playing','logging failure does not stop playback or later logging');
    console.log('Diagnostics observes media events without player writes; batches, queue and failures are bounded');
})().catch(error=>{console.error(error);process.exitCode=1});
