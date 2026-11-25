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
    currentSourcePath: null, queueStateUrl: () => "/api/state", renderClipQueue() {},
    document:{visibilityState:'visible',
        getElementById(id){
            if(!elements.has(id)) elements.set(id,{scrollTop:40,children:[],textContent:'',
                set innerHTML(value){if(id==='files')rows++;this.children=[]},
                classList:{values:new Set(),toggle(k,v){v?this.values.add(k):this.values.delete(k)},contains(k){return this.values.has(k)}},
                appendChild(child){this.children.push(child)}});
            return elements.get(id);
        },
        createElement(){return {setAttribute(name,value){this[name]=value}}},
        addEventListener(name,fn){events[name]=fn},
    },
    window:{addEventListener(name,fn){events[name]=fn}},
    setInterval(fn,ms){assert.equal(ms,5000);timers.set(++nextTimer,fn);return nextTimer},
    clearInterval(id){timers.delete(id)},
    escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
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
    assert.equal(elements.get('files').children[0].title,'transfers');
    elements.get('files').children[0].onclick();
    const row=elements.get('files').children[0];
    assert.equal(row.title,'transfers/new.MP4');
    assert.equal(row.innerHTML.split('new.MP4').length-1,1,'filename appears once');
    assert.match(row.innerHTML,/1\.5 GB · Length: reading…/);
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
    assert.equal(elements.get('files').children.length,0);
    ctx.openParentFolder();
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

    // Browse one directory at a time, with duplicate names and prefix collisions.
    state.source_path='';
    const item=(path)=>({name:path.split('/').pop(),path,size_bytes:1500000000,duration_seconds:65});
    files=[item('Camera/2026-11-22/clip.mp4'),item('Camera/2026-11-23/clip.mp4'),
           item('Camera/direct.mp4'),item('Camera2/other.mp4'),item('root.mp4'),
           item('<camera & friends>/a.mp4')];
    state.clip_counts={'Camera/2026-11-22/clip.mp4':2};
    await ctx.loadFiles();
    const listing=()=>elements.get('files').children;
    const at=path=>listing().find(row=>row.title===path);
    assert.deepEqual(listing().map(row=>row.title),['<camera & friends>','Camera','Camera2','root.mp4']);
    assert.match(at('Camera').innerHTML,/3 videos/);
    assert.ok(!at('Camera/2026-11-22/clip.mp4'),'nested videos hidden at root');
    assert.match(at('<camera & friends>').innerHTML,/&lt;camera &amp; friends&gt;/);
    const previousSelections=selections;
    at('Camera').onclick();
    assert.equal(selections,previousSelections,'folder opens never select or queue a video');
    assert.equal(elements.get('folderUp').disabled,false);
    assert.deepEqual(listing().map(row=>row.title),['Camera/2026-11-22','Camera/2026-11-23','Camera/direct.mp4']);
    assert.ok(!at('Camera2/other.mp4'),'similar prefix is a separate folder');
    at('Camera/2026-11-22').onclick();
    assert.equal(listing().length,1);
    assert.match(listing()[0].innerHTML,/clip.mp4 — 2 queued clips/);
    assert.match(listing()[0].innerHTML,/1\.5 GB · 01:05/);
    let selected;
    ctx.selectVideo=file=>{selected=file;return Promise.resolve()};
    listing()[0].onclick();
    assert.equal(selected.path,'Camera/2026-11-22/clip.mp4');
    assert.deepEqual(elements.get('folderBreadcrumbs').children.filter(c=>c.type==='button').map(c=>c.textContent),
                     ['Videos','Camera','2026-11-22']);
    const beforeRefresh=rows;
    await ctx.loadFiles();
    assert.equal(rows,beforeRefresh,'polling does not replace unchanged folder rows');
    files.push(item('Camera/2026-11-22/new.mp4'),item('Other/new.mp4'));
    await ctx.loadFiles();
    assert.deepEqual(listing().map(row=>row.title),['Camera/2026-11-22/clip.mp4','Camera/2026-11-22/new.mp4']);
    fail=true;await ctx.loadFiles();fail=false;
    assert.equal(listing().length,2,'outage preserves folder and its contents');
    files=files.filter(file=>!file.path.startsWith('Camera/2026-11-22/'));
    await ctx.loadFiles();
    assert.equal(listing().length,0,'deleting last video leaves an empty folder view');
    assert.equal(elements.get('folderEmpty').classList.contains('hidden'),false);
    assert.equal(elements.get('folderUp').disabled,false,'empty folder still has a way back');
    ctx.openParentFolder();
    assert.deepEqual(listing().map(row=>row.title),['Camera/2026-11-23','Camera/direct.mp4']);
    at('Camera/2026-11-23').onclick();
    elements.get('folderBreadcrumbs').children.find(c=>c.textContent==='Camera').onclick();
    assert.ok(at('Camera/2026-11-23'),'breadcrumb navigates to parent');
    elements.get('folderBreadcrumbs').children.find(c=>c.textContent==='Videos').onclick();
    assert.ok(at('root.mp4'),'root breadcrumb returns to library');
    assert.equal(elements.get('folderUp').disabled,true);
    assert.equal(elements.get('folderEmpty').classList.contains('hidden'),true);

    console.log('Folder navigation, metadata, refresh, rename/delete, outage, resume and page-return checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
