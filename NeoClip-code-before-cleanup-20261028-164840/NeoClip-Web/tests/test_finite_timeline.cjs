// Exercise the shipped attachment, including Safari MMS capability fallback.
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const code=html.slice(html.indexOf('    let releaseFiniteTimeline'),html.indexOf('    let markInSeconds'));
const wait=html.slice(html.indexOf('// CONTINUOUS_WAIT_START'),html.indexOf('// CONTINUOUS_WAIT_END'));
function setup({supported=true,apple=true,duration=600,growing=true}={}) {
 const mediaEvents={},calls=[],sources=[];
 class Hls {
  static isSupported(){return supported}
  static Events={MEDIA_ATTACHED:'attached',LEVEL_UPDATED:'level',MANIFEST_PARSED:'manifest',ERROR:'error'};
  static ErrorTypes={NETWORK_ERROR:'network',MEDIA_ERROR:'media'};
  constructor(config){this.config=config;this.events={};calls.push(['create',config]);}
  on(n,fn){this.events[n]=fn;if(n==='manifest')fn();}
  loadSource(url){calls.push(['load',url]);}
  attachMedia(arg){this.attachment=arg;this.media=arg.media||arg;calls.push(['attach',arg]);ctx.video.disableRemotePlayback=true;this.events.attached?.();}
  detachMedia(){this.media=null;calls.push(['detach']);}
  startLoad(time){calls.push(['start',time]);}
  destroy(){calls.push(['destroy']);}
 }
 const ctx={window:{Hls},Hls,navigator:{userAgent:apple?'iPhone Safari':'Chrome'},
  currentProxy:{id:'one',generation:'one',continuous:true,duration,playable:true,
   build_status:growing?'building':'complete',available_end:growing?18:duration},
  hls:null,proxyOffset:0,pendingResumeSeconds:0,setTimeout,clearTimeout,
  document:{createElement(){return {remove(){sources.splice(sources.indexOf(this),1)}}}},
  video:{readyState:2,controls:true,disableRemotePlayback:false,currentTime:2,paused:false,
   canPlayType(){return 'probably'},querySelector(){return {}},appendChild(s){sources.push(s)},
   addEventListener(n,fn){mediaEvents[n]=fn},removeEventListener(n){delete mediaEvents[n]},
   load(){mediaEvents.loadedmetadata?.()},
  },
 };
 vm.createContext(ctx);vm.runInContext(code+wait,ctx);
 return {ctx,calls,sources,mediaEvents};
}
(async()=>{
 const x=setup();const url='/api/vod/playlist.m3u8?id=one&growing=true';
 await x.ctx.setVideoSrc(url);
 const engine=x.ctx.hls;
 assert.equal(engine.config.liveDurationInfinity,false);
 assert.equal(engine.config.preferManagedMediaSource,true);
 assert.equal(engine.config.maxLiveSyncPlaybackRate,1);
 assert.equal(engine.config.lowLatencyMode,false);
 assert.equal(engine.attachment.media,x.ctx.video);
 assert.equal(engine.attachment.overrides.duration,600);
 assert.equal(x.ctx.video.controls,true);
 assert.equal(x.ctx.video.disableRemotePlayback,false);
 assert.equal(x.sources.length,1);assert.equal(x.sources[0].src,url);
 assert.equal(x.ctx.continuousHasTime(x.ctx.currentProxy,0),true);
 assert.equal(x.ctx.continuousHasTime(x.ctx.currentProxy,1),false);
 // Growing and completion metadata update duration without source/position/play changes.
 const count=x.calls.length;
 engine.events.level(null,{details:{live:true,edge:24}});
 assert.equal(engine.attachment.overrides.duration,600);
 engine.events.level(null,{details:{live:false,edge:599.9}});
 assert.equal(engine.attachment.overrides.duration,599.9);
 assert.equal(x.calls.length,count);assert.equal(x.ctx.video.currentTime,2);
 assert.equal(x.ctx.video.paused,false);
 engine.events.error(null,{fatal:true,type:'media'});
 assert.equal(engine.attachment.overrides.duration,599.9,'recovery retains finite duration');
 assert.equal(x.sources.length,1,'recovery replaces AirPlay alternative rather than accumulating it');
 // Returning from AirPlay must restore local finite playback, once.
 engine.detachMedia();x.ctx.video.webkitCurrentPlaybackTargetIsWireless=true;
 const afterRemote=x.calls.length;
 x.mediaEvents.webkitcurrentplaybacktargetiswirelesschanged();
 assert.equal(x.calls.length,afterRemote);
 x.ctx.video.paused=true;x.ctx.video.webkitCurrentPlaybackTargetIsWireless=false;
 x.mediaEvents.webkitcurrentplaybacktargetiswirelesschanged();
 assert.equal(engine.attachment.overrides.duration,599.9);
 assert.deepEqual(x.calls.at(-1),['start',2]);
 assert.equal(x.sources.length,1);
 x.ctx.destroyHls();assert.equal(x.sources.length,0);
 assert.ok(!x.mediaEvents.webkitcurrentplaybacktargetiswirelesschanged);
 assert.ok(!x.mediaEvents.canplay);
 assert.equal(x.ctx.video.disableRemotePlayback,false);
 const old=setup({supported:false});
 assert.equal(old.ctx.continuousHasTime(old.ctx.currentProxy,0),false,'old Safari waits for finite VOD');
 await assert.rejects(old.ctx.setVideoSrc(url),/completed proxy/);
 old.ctx.currentProxy.build_status='complete';
 assert.equal(old.ctx.continuousHasTime(old.ctx.currentProxy,0),true);
 await old.ctx.setVideoSrc('/api/vod/playlist.m3u8?id=one');
 assert.equal(old.calls.length,0);assert.match(old.ctx.video.src,/playlist/);
 const complete=setup({growing:false});await complete.ctx.setVideoSrc('/api/vod/playlist.m3u8?id=one');
 assert.equal(complete.calls.length,0,'complete Safari proxies remain native HLS');
 const original=setup();await original.ctx.setVideoSrc('/api/media?path=source.mp4');
 assert.equal(original.calls.length,0,'original file stays native with normal controls');
 assert.equal(original.ctx.video.controls,true);
 const missingDuration=setup({duration:Infinity});
 assert.equal(missingDuration.ctx.continuousHasTime(missingDuration.ctx.currentProxy,0),false);
 await assert.rejects(missingDuration.ctx.setVideoSrc(url),/completed proxy/);
 const chrome=setup({apple:false});await chrome.ctx.setVideoSrc(url);
 assert.equal(chrome.ctx.hls.attachment.overrides.duration,600);
 assert.equal(chrome.ctx.hls.config.preferManagedMediaSource,false);
 console.log('Finite native timeline: growing/cached/original routes, Safari fallback, completion, AirPlay cleanup and recovery passed');
})().catch(e=>{console.error(e);process.exitCode=1});
