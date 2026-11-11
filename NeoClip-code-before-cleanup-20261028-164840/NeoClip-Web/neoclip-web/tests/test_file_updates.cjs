const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html=readFileSync(`${__dirname}/../app/web/index.html`,'utf8');
const elements=new Map(), events={}, timers=new Map();
let nextTimer=0, scans=0, rows=0, selections=0, release;
let files=[], fail=false, held=false;
let scan={status:'ready',unavailable:[]};
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
        return {files,scan};
    },
};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('    let fileLoadPromise'),html.indexOf('    let proxyWatchToken')),ctx);
const tick=async()=>{for(const fn of timers.values())fn();await ctx.loadFiles()};
(async()=>{
    ctx.startFileUpdates();ctx.startFileUpdates();
    assert.equal(timers.size,1);
    await ctx.loadFiles({restoreLastSession:true});
    files=[{name:'new.MP4',path:'transfers/new.MP4',size_bytes:1500000000,modified_ns:'1',duration_seconds:null,duration_pending:true}];
    await tick();
    const row=elements.get('files').children[0];
    assert.equal(row.title,'transfers/new.MP4');
    assert.equal(row.innerHTML.split('new.MP4').length-1,1,'filename appears once');
    assert.match(row.innerHTML,/1\.5 GB · Length: reading… · Folder: transfers/);
    assert.equal(ctx.formatFileSize(0),'0 B');
    assert.equal(ctx.formatFileSize(undefined),'Size unavailable');
    assert.equal(ctx.formatFileDuration({duration_seconds:3600}),'1:00:00');
    assert.equal(ctx.formatFileDuration({duration_seconds:59.9}),'01:00');
    assert.equal(ctx.formatFileDuration({duration_seconds:null}),'Length unavailable');
    files=[{...files[0],duration_seconds:5025.4,duration_pending:false}];
    await tick();
    assert.match(elements.get('files').children[0].innerHTML,/1:23:45/);
    scan={status:'partial',unavailable:['stalled-folder']};
    await tick();
    assert.match(elements.get('fileStatus').textContent,/stalled-folder/);
    assert.equal(elements.get('files').children.length,1,'unavailable folder keeps existing videos');
    scan={status:'ready',unavailable:[]};
    const unchanged=rows;
    await tick();assert.equal(rows,unchanged,'unchanged polls preserve DOM/focus');
    files=[{...files[0],size_bytes:25}];await tick();
    assert.equal(rows,unchanged+1,'growing/replaced files change the snapshot');
    files=[{...files[0],name:'renamed.mp4',path:'renamed.mp4'}];await tick();
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
    state.source_path='later.mp4';
    scan={status:'scanning',unavailable:[]};files=[];
    await ctx.loadFiles({restoreLastSession:true});
    assert.equal(selections,0);
    files=[{name:'later.mp4',path:'later.mp4',size_bytes:2}];
    let finishSelection;
    ctx.selectVideo=()=>{selections++;return new Promise(resolve=>{finishSelection=resolve})};
    await ctx.loadFiles();
    assert.equal(selections,1,'resume follows a video discovered on a later scan');
    files=[...files,{name:'other.mp4',path:'other.mp4',size_bytes:3}];
    await ctx.loadFiles();
    assert.equal(elements.get('files').children.length,2,'slow preview cannot block file-list updates');
    finishSelection();
    console.log('Automatic transfer/rename/change/delete refresh, retry, dedup and page-return checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
