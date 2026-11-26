const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const code = html.slice(html.indexOf('// RECOVERY_CONTROLLER_START'), html.indexOf('// RECOVERY_CONTROLLER_END'));
const flush = async () => {for(let i=0;i<20;i++) await Promise.resolve()};
function setup(connect = async () => {}) {
    const timers = new Map(), reports = [], calls = [];
    let serial=0;
    const state={path:'movie.mkv', current_time:29, autoplay:false, mark_in:10, mark_out:40, clip_name:'Keep'};
    const ctx={setTimeout(fn,ms){timers.set(++serial,{fn,ms});return serial},clearTimeout(id){timers.delete(id)}};
    vm.createContext(ctx);vm.runInContext(code,ctx);
    const s={visible:true,state,reports,calls,timers,ctx};
    s.controller=ctx.createPreviewRecovery({snapshot:()=>s.state,visible:()=>s.visible,
        connect:async(saved,valid)=>{calls.push({...saved});await connect(saved,valid)},
        report:(...args)=>reports.push(args)});
    s.tick=async()=>{const first=[...timers.entries()].find(([,t])=>t.ms<30000);assert.ok(first,'retry scheduled');timers.delete(first[0]);first[1].fn();await flush()};
    return s;
}
(async()=>{
    const idle=setup();idle.controller.resume();assert.equal(idle.timers.size,0,'healthy paused preview is not reloaded');
    idle.controller.request();idle.controller.request();assert.equal(idle.timers.size,1,'coalesce duplicate errors');
    await idle.tick();assert.deepEqual(idle.calls[0],idle.state);assert.equal(idle.calls[0].autoplay,false);
    assert.equal(idle.controller.pending(),false);assert.equal(idle.reports.at(-1)[0],'ready');
    const locked=setup();locked.visible=false;locked.controller.request();assert.equal(locked.timers.size,0);
    locked.visible=true;locked.controller.resume();await locked.tick();assert.equal(locked.calls.length,1,'recover when phone returns');
    const offline=setup(async()=>{throw new Error('offline')});offline.controller.request();
    for(let i=0;i<3;i++)await offline.tick();
    assert.equal(offline.calls.length,3);assert.equal(offline.timers.size,0);assert.equal(offline.reports.at(-1)[0],'failed');
    offline.controller.request();offline.controller.resume();assert.equal(offline.timers.size,0,'events cannot reset exhausted budget');
    offline.controller.request({manual:true,current_time:0,autoplay:true});await offline.tick();
    assert.equal(offline.calls.at(-1).current_time,0,'manual rewind to zero survives recovery');
    assert.equal(offline.calls.at(-1).autoplay,true);
    const flapping=setup();
    for(let i=0;i<3;i++){flapping.controller.request();await flapping.tick()}
    flapping.controller.request();assert.equal(flapping.timers.size,0,'brief successes do not cause infinite error/reload loops');
    const healthy=setup();healthy.controller.request();await healthy.tick();healthy.controller.progress(32);
    healthy.controller.request();await healthy.tick();assert.equal(healthy.calls.length,2);
    let finish;const stale=setup(async(saved,valid)=>{await new Promise(r=>finish=r);assert.equal(valid(),false)});
    stale.controller.request();await stale.tick();stale.controller.cancel();stale.state={...stale.state,path:'other.mp4'};
    finish();await flush();assert.equal(stale.reports.filter(r=>r[0]==='ready').length,0,'selection/delete invalidates in-flight recovery');
    assert.equal(stale.timers.size,0);
    console.log('Native preview recovery: paused state, lock/wake, bounded retries, manual replay, stable reset and stale cancellation passed');
})().catch(error=>{console.error(error);process.exitCode=1});
