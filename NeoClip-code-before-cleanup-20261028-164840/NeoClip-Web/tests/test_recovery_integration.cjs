const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const code=html.slice(html.indexOf('// RECOVERY_CONTROLLER_START'),html.indexOf('    function startPlaybackWatchdog()'));
const continuous=html.slice(html.indexOf('// CONTINUOUS_WAIT_START'),html.indexOf('// CONTINUOUS_WAIT_END'));
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
function setup({autoplay=false, fail=false}={}) {
 const timers=new Map(),events={},elements=new Map(),calls=[];
 let serial=0;
 const video={currentTime:29,paused:!autoplay,error:{code:3},readyState:2,
  addEventListener(n,f){(events[n]||=[]).push(f)},
  async play(){this.paused=false;calls.push('play')},pause(){this.paused=true;calls.push('pause')}};
 const ctx={video,userWantsPlayback:autoplay,playbackEnded:false,proxyWatchToken:1,
  currentSourcePath:'movie.mkv',activeFile:{path:'movie.mkv'},deletionBusy:false,selectionBusy:false,
  currentlyUsingProxy:true,restoringPlayhead:false,pendingResumeSeconds:29,proxyOffset:0,sourceDuration:277,
  currentProxy:{id:'old'},markInSeconds:10,markOutSeconds:40,
  setTimeout(fn,ms){timers.set(++serial,{fn,ms});return serial},clearTimeout(id){timers.delete(id)},
  document:{visibilityState:'visible',addEventListener(n,f){(events[n]||=[]).push(f)},
   getElementById(id){if(!elements.has(id))elements.set(id,{textContent:'',classList:{toggle(){}}});return elements.get(id)}},
  window:{addEventListener(n,f){(events[n]||=[]).push(f)}},
  editorSessionPayload:()=>({path:ctx.currentSourcePath,current_time:ctx.pendingResumeSeconds,mark_in:10,mark_out:40,clip_name:'Keep'}),
  usesNativeHls:()=>true,sourceTime:()=>video.currentTime,resetPlayerLoading(){},setPlayerLoading(){},clearPlayerLoading(){},
  startHeartbeat(){calls.push('heartbeat')},showPipeline(){},updateSourceClock(){},scheduleEditorSessionSave(){calls.push('save')},
  async api(url,options){
   calls.push(url);assert.equal(url,'/api/proxy/start');assert.equal(options.timeoutMs,10000);
   assert.deepEqual(JSON.parse(options.body),{path:'movie.mkv',current_time:29,native_vod:true});
   if(fail)throw Error('Server unavailable');
   return {id:'cached',url:'/api/vod/playlist.m3u8?id=cached',start_seconds:0,duration:277,playable:true};
  },
  async setVideoSrc(url){calls.push(url);video.error=null;video.currentTime=0},
  async seekVideoToSavedTime(t,token){assert.equal(token,ctx.proxyWatchToken);calls.push(['seek',t]);video.currentTime=t},
 };
 vm.createContext(ctx);vm.runInContext(continuous+code+'\nglobalThis.controller = previewRecovery;',ctx);
 return {ctx,calls,elements,timers,events,async event(n){for(const f of events[n]||[])f();await flush()},
  async tick(){const first=[...timers].find(([,v])=>v.ms<30000);assert.ok(first);timers.delete(first[0]);first[1].fn();await flush()}};
}
(async()=>{
 for(const autoplay of [false,true]) {
  const s=setup({autoplay});await s.event('error');assert.equal(s.ctx.restoringPlayhead,true);
  await s.tick();assert.equal(s.ctx.video.currentTime,29);assert.equal(s.ctx.video.paused,!autoplay);
  assert.equal(s.ctx.markInSeconds,10);assert.equal(s.ctx.markOutSeconds,40);
  assert.equal(s.ctx.restoringPlayhead,false);assert.equal(s.ctx.currentProxy.id,'cached');
  assert.equal(s.calls.filter(c=>c==='/api/proxy/start').length,1);
  assert.ok(s.calls.includes('save'));assert.match(s.elements.get('selectedFile').textContent,/restored/);
 }
 const failed=setup({fail:true});await failed.event('error');await failed.tick();
 assert.equal(failed.ctx.pendingResumeSeconds,29);assert.equal(failed.ctx.restoringPlayhead,true,'failed recovery protects saved position');
 const locked=setup();locked.ctx.document.visibilityState='hidden';await locked.event('error');assert.equal(locked.timers.size,0);
 locked.ctx.document.visibilityState='visible';await locked.event('visibilitychange');await locked.tick();
 assert.equal(locked.ctx.video.currentTime,29);
 const healthy=setup();healthy.ctx.video.error=null;await healthy.event('visibilitychange');assert.equal(healthy.timers.size,0);
 console.log('Recovery integration: native media error, cached proxy reuse, lock/wake, paused/playing intent and edits preserved');
})().catch(e=>{console.error(e);process.exitCode=1});
