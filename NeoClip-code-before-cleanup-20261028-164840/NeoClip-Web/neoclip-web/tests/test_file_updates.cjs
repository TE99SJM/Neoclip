const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const elements=new Map(), events={}, timers=new Map();
let nextTimer=0, scans=0, rows=0, selections=0, release;
let files=[], fail=false, held=false;
const state={clip_counts:{},total_queued_clips:0,source_path:'',current_session:{}};
const ctx={
    document:{visibilityState:'visible',
        getElementById(id){
            if(!elements.has(id)) elements.set(id,{scrollTop:40,children:[],textContent:'',
                set innerHTML(value){rows++;this.children=[]},
                appendChild(child){this.children.push(child)}});
            return elements.get(id);
        },
        createElement(){return {}},
        addEventListener(name,fn){events[name]=fn},
    },
    window:{addEventListener(name,fn){events[name]=fn}},
    setInterval(fn,ms){assert.equal(ms,5000);timers.set(++nextTimer,fn);return nextTimer},
    clearInterval(id){timers.delete(id)},
    escapeHtml:s=>s,
    async selectVideo(){selections++},
    async api(url,options){
        assert.equal(options.cache,'no-store');
        if(url==='/api/state') return state;
        scans++;
        if(held) await new Promise(resolve=>{release=resolve});
        if(fail) throw new Error('NAS temporarily unavailable');
        return files;
    },
};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('    let fileLoadPromise'),html.indexOf('    let proxyWatchToken')),ctx);
const tick=async()=>{for(const fn of timers.values())fn();await ctx.loadFiles()};
(async()=>{
    ctx.startFileUpdates();ctx.startFileUpdates();
    assert.equal(timers.size,1);
    await ctx.loadFiles({restoreLastSession:true});
    files=[{name:'new.MP4',path:'transfers/new.MP4',size_bytes:12,modified_ns:'1'}];
    await tick();
    assert.match(elements.get('files').children[0].innerHTML,/transfers\/new.MP4/);
    const unchanged=rows;
    await tick();assert.equal(rows,unchanged,'unchanged polls preserve DOM/focus');
    files=[{...files[0],size_bytes:25}];await tick();
    assert.equal(rows,unchanged+1,'growing/replaced files change the snapshot');
    files=[{...files[0],path:'renamed.mp4'}];await tick();
    assert.match(elements.get('files').children[0].innerHTML,/renamed.mp4/);
    fail=true;await tick();
    assert.equal(elements.get('files').children.length,1,'failed scan preserves listing');
    assert.match(elements.get('fileStatus').textContent,/retry/);
    fail=false;files=[];await tick();
    assert.equal(elements.get('files').children.length,0);
    assert.equal(selections,0,'background changes never reset the player');
    held=true;
    const before=scans;
    const first=ctx.loadFiles(),second=ctx.loadFiles();
    assert.equal(scans,before+1,'overlapping ticks share a scan');
    release();await Promise.all([first,second]);held=false;
    events.pagehide();assert.equal(timers.size,0);
    events.pageshow({persisted:true});await ctx.loadFiles();assert.equal(timers.size,1);
    events.pagehide();
    console.log('Automatic transfer/rename/change/delete refresh, retry, dedup and page-return checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
