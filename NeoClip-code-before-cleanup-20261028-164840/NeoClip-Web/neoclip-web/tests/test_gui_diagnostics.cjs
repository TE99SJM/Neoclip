const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const source=readFileSync(`${__dirname}/../app/web/test-diagnostics.js`,'utf8');
function target(object={}){
 const listeners=new Map();
 return Object.assign(object,{
  listeners,
  addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn)},
  removeEventListener(name,fn){listeners.get(name)?.delete(fn)},
  emit(name,event={}){return Promise.all([...(listeners.get(name)||[])].map(fn=>fn(event)))},
 });
}
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.textContent='';this.value='';target(this)}
 appendChild(child){child.parent=this;this.children.push(child);return child}
 insertBefore(child,before){child.parent=this;const i=this.children.indexOf(before);this.children.splice(i<0?0:i,0,child)}
 get firstChild(){return this.children[0]}
 setAttribute(name,value){this[name]=value}
 remove(){if(this.parent)this.parent.children.splice(this.parent.children.indexOf(this),1)}
 focus(){this.focused=true} select(){this.selected=true}
}
function setup(enabled){
 const main=new Element('main'),version=new Element('p');version.className='version';version.textContent='V2.2.0-playback.3';main.appendChild(version);
 const nodes={};for(const id of ['selectedFile','pipelineStatus','fileStatus','renderStatus','speedStatus']){nodes[id]=new Element('p');nodes[id].id=id;}
 const all=node=>[node,...node.children.flatMap(all)];
 const document=target({documentElement:{dataset:{testDiagnostics:String(enabled)}},visibilityState:'visible',
  createElement:tag=>new Element(tag),querySelector:q=>q==='main'?main:q==='.version'?version:null,
  getElementById:id=>all(main).find(n=>n.id===id)||nodes[id]||null,
 });
 const raw=target({currentTime:12,currentSrc:'/preview',paused:false,ended:false,seeking:false,readyState:4,playbackRate:1,controls:true,error:null});
 const video=new Proxy(raw,{set(){throw Error('Diagnostics wrote to the player')},get(t,key){if(['play','pause','load'].includes(key))return ()=>assert.fail('Playback method called');return t[key]}});
 const window=target({});let proxy=null;const intervals=new Map();let intervalId=0;
 const navigator={onLine:true};
 vm.runInNewContext(source,{window,document,navigator,Date,Map,Number,String,JSON,
  setInterval(fn){const id=++intervalId;intervals.set(id,fn);return id},clearInterval:id=>intervals.delete(id),
  fetch(){assert.fail('GUI diagnostics should not make network requests')},
 });
 const api=window.installNeoClipTestDiagnostics({video,getProxy:()=>proxy});
 return {api,main,document,window,navigator,raw,video,intervals,nodes,
  setProxy:p=>proxy=p,update(){for(const fn of intervals.values())fn()},all};
}
(async()=>{
 const off=setup(false);assert.equal(off.api,null);assert.equal(off.main.children.length,1);
 assert.equal(off.raw.listeners.size,0);assert.equal(off.window.listeners.size,0);assert.equal(off.intervals.size,0);
 const x=setup(true);const panel=x.main.firstChild;
 assert.equal(panel.id,'testDiagnostics');assert.equal(x.intervals.size,1);
 const children=x.all(panel),log=children.find(n=>n.tagName==='textarea'),summary=children.find(n=>n.tagName==='summary');
 const clear=children.find(n=>n.textContent==='Clear log'),copy=children.find(n=>n.textContent==='Copy log');
 assert.ok(children.some(n=>n.textContent==='Player: Playing'));
 assert.equal(x.window.installNeoClipTestDiagnostics({video:x.video,getProxy:()=>null}),null);
 assert.equal(x.intervals.size,1);
 x.setProxy({id:'a',generation:'b',build_status:'building',progress:42,pipeline:'GPU'});x.update();
 assert.ok(children.some(n=>n.textContent==='Proxy: building 42%'));
 const unchanged=log.value;for(let i=0;i<100;i++)x.update();assert.equal(log.value,unchanged,'unchanged status should not flood the log');
 x.raw.error={code:3,message:'Decode failed'};await x.raw.emit('error');
 assert.ok(children.some(n=>n.textContent==='Player: Error 3'));
 assert.ok(log.value.includes('code=3'));
 x.setProxy({id:'a',build_status:'retrying',error:'Encoder failed'});x.update();
 assert.ok(log.value.includes('[Proxy error]'));
 x.nodes.selectedFile.textContent='<img src=x onerror=alert(1)> could not load';x.update();
 assert.ok(log.value.includes('<img src=x'));
 assert.equal(x.all(panel).some(n=>n.tagName==='img'),false,'errors are rendered as text only');
 await x.window.emit('unhandledrejection',{reason:new Error('Request timed out')});
 assert.ok(log.value.includes('[Request error]'));
 for(let i=0;i<140;i++)await x.raw.emit('stalled');
 assert.equal(log.value.split('\n').length,100);assert.equal(summary.textContent,'Recent events (100)');
 x.document.visibilityState='hidden';await x.document.emit('visibilitychange');assert.equal(x.intervals.size,0);
 x.document.visibilityState='visible';await x.document.emit('visibilitychange');await x.window.emit('pageshow');assert.equal(x.intervals.size,1);
 await copy.emit('click');assert.equal(log.selected,true,'HTTP clipboard fallback selects the log');
 let copied='';x.navigator.clipboard={async writeText(text){copied=text}};await copy.emit('click');assert.ok(copied.startsWith('V2.2.0-playback.3\n'));
 await clear.emit('click');assert.equal(log.value,'');assert.ok(children.some(n=>n.textContent==='Errors: 0'));
 assert.equal(x.raw.currentTime,12);assert.equal(x.raw.controls,true);assert.equal(x.raw.paused,false);
 x.api.dispose();assert.equal(x.intervals.size,0);assert.equal(x.main.children.length,1);
 for(const t of [x.raw,x.window,x.document])for(const set of t.listeners.values())assert.equal(set.size,0);
 console.log('GUI diagnostics: default off, top status, bounded safe log, proxy/errors, deduplication, visibility, clipboard and zero playback writes passed');
})().catch(error=>{console.error(error);process.exitCode=1});
