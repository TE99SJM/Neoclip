const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const waitCode=html.slice(html.indexOf('// CONTINUOUS_WAIT_START'),html.indexOf('// CONTINUOUS_WAIT_END'));
const watchCode=html.slice(html.indexOf('    async function watchProxyAndSwitch('),html.indexOf('    async function switchToProxy('));
const heartbeatCode=html.slice(html.indexOf('    function startHeartbeat('),html.indexOf('    // If playback enters'));
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
(async()=>{
 for(const resume of [0,12]) {
  const calls=[],events={},messages=[];
  let polls=0;
  const ctx={proxyWatchToken:1,currentlyUsingProxy:false,pendingResumeSeconds:resume,
   currentProxy:{id:'one'},video:{addEventListener(n,f){events[n]=f}},
   document:{getElementById(){return {set textContent(s){messages.push(s)}}}},
   showPipeline(){},resetPlayerLoading(){},setPlayerLoading(){},formatClock:String,
   proxyStatusUrl:()=>'/status',setTimeout(fn){queueMicrotask(fn)},
   async api(){polls++;assert.ok(polls<10);return {id:'one',continuous:true,build_status:'building',
     playable:polls>=3,available_end:polls*6,switch_lead_seconds:18};},
   async switchToProxy(file,status,token){calls.push({status,token});ctx.currentlyUsingProxy=true;return true;},
  };
  vm.createContext(ctx);vm.runInContext(waitCode+watchCode,ctx);
  await ctx.watchProxyAndSwitch({path:'movie.mkv'},1);
  assert.equal(polls,(resume+18)/6,'start/resume waits for safe lead, not full encoding');
  assert.equal(calls.length,1);assert.equal(calls[0].status.build_status,'building');
 }
 // Completion arrives via the existing heartbeat. Neither src nor currentTime
 // may be written: the native EVENT attachment learns ENDLIST by reloading HLS.
 let ping,complete=false;
 const before={currentTime:7,src:'/api/vod/playlist.m3u8?id=one&growing=true',paused:false};
 const video=new Proxy(before,{set(){throw Error('Healthy playback was modified')}});
 const ctx={video,currentProxy:{id:'one'},currentSourcePath:'movie.mkv',sourceDuration:36,heartbeatTimer:null,
  setInterval(fn){ping=fn;return 1},clearInterval(){},
  proxyStatusUrl:()=>'/status',adoptRenamedVideo:()=>false,showPipeline(){},
  document:{getElementById(){return {textContent:''}}},
  async api(){return {id:'one',path:'movie.mkv',continuous:true,status:'ready',duration:complete?35.9:36,
    build_status:complete?'complete':'building',url:complete?'/vod':'/event'}},
  setVideoSrc(){throw Error('Healthy source was replaced')},
 };
 vm.createContext(ctx);vm.runInContext(heartbeatCode,ctx);
 ctx.startHeartbeat('movie.mkv');await flush();complete=true;await ping();
 assert.equal(ctx.currentProxy.build_status,'complete');assert.equal(ctx.sourceDuration,35.9);
 assert.deepEqual(before,{currentTime:7,src:'/api/vod/playlist.m3u8?id=one&growing=true',paused:false});
 console.log('Growing playback: early startup, saved-time lead, completion without reattachment passed');
})().catch(e=>{console.error(e);process.exitCode=1});
