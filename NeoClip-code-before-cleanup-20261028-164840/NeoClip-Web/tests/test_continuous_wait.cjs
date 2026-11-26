const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const code=html.slice(html.indexOf('// CONTINUOUS_WAIT_START'),html.indexOf('// CONTINUOUS_WAIT_END'));
function setup() {
 const events={}, calls=[], messages=[];
 const ctx={currentProxy:{id:'job',generation:'one',continuous:true,playable:true,build_status:'complete',available_end:180},
  activeFile:{path:'movie.mkv'},selectionBusy:false,deletionBusy:false,proxyWatchToken:1,
  sourceDuration:180,proxyOffset:0,userWantsPlayback:true,currentlyUsingProxy:true,
  restoringPlayhead:false,pendingResumeSeconds:0,playbackEnded:false,
  seekIntentActive:false,seekIntentTarget:0,
  previewRecovery:{cancel(){}},updatePlaybackButton(){},formatClock:String,
  video:{currentTime:2,paused:false,error:null,addEventListener(n,f){events[n]=f},
   pause(){this.paused=true;calls.push('pause')},async play(){this.paused=false;calls.push('play')}},
  document:{getElementById(){return {set textContent(s){messages.push(s)}}}},
  setTimeout(fn){queueMicrotask(fn)},setPlayerLoading(){},clearPlayerLoading(){},
  scheduleEditorSessionSave(){calls.push('save')},proxyStatusUrl:()=>'/status',
  async api(){calls.push('status');return {...ctx.currentProxy,playable:true,build_status:'complete',available_end:180}},
  async setVideoSrc(){calls.push('attach');ctx.video.error=null},
  async seekVideoToSavedTime(t){calls.push(['seek',t]);ctx.video.currentTime=t},
  legacySeekSource(){calls.push('legacy')},
 };
 vm.createContext(ctx);vm.runInContext(code+'\nattachedContinuousGeneration="one";',ctx);
 return {ctx,calls,events,messages};
}
(async()=>{
 const early=setup();Object.assign(early.ctx.currentProxy,{build_status:'building',available_end:18,switch_lead_seconds:18});
 assert.equal(early.ctx.continuousHasTime(early.ctx.currentProxy,0),true);
 assert.equal(early.ctx.continuousHasTime(early.ctx.currentProxy,1),false,'resume needs encoded lead');
 await early.ctx.seekSource(0);assert.deepEqual(early.calls,[['seek',0],'play','save']);
 const ready=setup();await ready.ctx.seekSource(3);
 assert.deepEqual(ready.calls,[['seek',3],'play','save']);
 const building=setup();Object.assign(building.ctx.currentProxy,{playable:false,build_status:'building',available_end:120});building.ctx.api=async()=>{building.calls.push('status');return {...building.ctx.currentProxy,playable:true,available_end:114}};await building.ctx.seekSource(90);
 assert.deepEqual(building.calls,['pause','status',['seek',90],'play','save']);
 assert.ok(building.messages.some(s=>s.includes('Preparing preview at 90')));
 assert.equal(building.ctx.pendingResumeSeconds,90);
 assert.equal(building.ctx.restoringPlayhead,false);
 const retry=setup();retry.ctx.video.error={code:3};await retry.ctx.seekSource(3,{autoplay:false});
 assert.deepEqual(retry.calls,['attach',['seek',3],'pause','save']);
 const generation=setup();generation.ctx.currentProxy.generation='two';await generation.ctx.seekSource(3);
 assert.equal(generation.calls.filter(c=>c==='attach').length,1);
 const cancelled=setup();Object.assign(cancelled.ctx.currentProxy,{playable:false,build_status:'building'});cancelled.ctx.api=async()=>{cancelled.ctx.proxyWatchToken++;return {...cancelled.ctx.currentProxy,available_end:120}};
 await cancelled.ctx.seekSource(90);
 assert.equal(cancelled.calls.some(Array.isArray),false,'old selection must not seek the new video');
 const native=setup();Object.assign(native.ctx.currentProxy,{playable:false,build_status:'building'});native.ctx.video.currentTime=90;native.events.seeking();
 for(let i=0;i<20;i++)await Promise.resolve();
 assert.ok(native.calls.some(c=>Array.isArray(c)&&c[1]===90),'native slider waits for coverage');
 assert.equal(native.calls.includes('attach'),false);
 const legacy=setup();legacy.ctx.currentProxy.continuous=false;await legacy.ctx.seekSource(3);
 assert.deepEqual(legacy.calls,['legacy']);
 console.log('Continuous preview: wait without reload, native seeks, cancellation, generation recovery and paused intent passed');
})().catch(e=>{console.error(e);process.exitCode=1});

// A second native slider gesture supersedes an outstanding missing-section wait.
(async()=>{
 const next=setup();Object.assign(next.ctx.currentProxy,{build_status:'building',playable:true,available_end:36});
 let release;
 next.ctx.api=()=>new Promise(resolve=>{release=resolve});
 next.ctx.video.currentTime=90;next.events.seeking();
 for(let i=0;i<10;i++)await Promise.resolve();
 assert.equal(next.ctx.pendingResumeSeconds,90);
 next.ctx.video.currentTime=2;next.events.seeking();
 for(let i=0;i<20;i++)await Promise.resolve();
 assert.ok(next.calls.some(c=>Array.isArray(c)&&c[1]===2));
 release({...next.ctx.currentProxy,build_status:'complete',available_end:180});
 for(let i=0;i<20;i++)await Promise.resolve();
 assert.equal(next.calls.some(c=>Array.isArray(c)&&c[1]===90),false);
 assert.equal(next.ctx.pendingResumeSeconds,2);
 console.log('Native slider: a later cached seek cancels the earlier build wait');
})().catch(e=>{console.error(e);process.exitCode=1});
