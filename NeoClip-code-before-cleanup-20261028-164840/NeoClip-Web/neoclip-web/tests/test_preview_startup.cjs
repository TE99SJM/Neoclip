const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
function setup() {
    const nodes = new Map();
    const ctx = {
        deletionBusy:false, selectionBusy:false, failedSelection:null, editorSessionReady:true,
        pendingSelection:null, proxyWatchToken:0, currentSourcePath:'old.mp4', activeFile:null,
        pendingResumeSeconds:14, currentlyUsingProxy:true, currentProxy:{id:'old'},
        restoringPlayhead:false, proxyOffset:0, sourceDuration:60, playbackEnded:false,
        requests:[], saved:[], spinner:false, watched:[], recovery:0, mode:'fail',
        document:{getElementById(id) {
            if (!nodes.has(id)) nodes.set(id,{textContent:'',hidden:false,
                classList:{add(){nodes.get(id).hidden=true},remove(){nodes.get(id).hidden=false}}});
            return nodes.get(id);
        }},
        api: async (url, options) => {
            ctx.requests.push({url, options});
            if (url === '/api/source') {
                assert.equal(ctx.editorSessionReady,false);
                assert.equal(ctx.editorSessionPayload(),null,'do not save an old draft under the pending source');
                if (ctx.mode === 'fail') throw Error('Reading video information timed out after 25 seconds');
                return {proxy:{id:'new',start_seconds:0,duration:60},current_session:{current_time:14}};
            }
            return {};
        },
        saveEditorSessionNow:async()=>{if(ctx.editorSessionReady)ctx.saved.push(ctx.currentSourcePath)},
        previewRecovery:{cancel(){},request(){ctx.recovery++}},
        cancelPendingSeeks(){},resetPlayerLoading(){ctx.spinner=false},setPlayerLoading(){ctx.spinner=true},
        stopHeartbeat(){},startHeartbeat(){},updatePlaybackButton(){},updateSourceClock(){},
        applyEditorSession(s){ctx.pendingResumeSeconds=s.current_time},
        destroyHls(){},video:{removeAttribute(){},load(){}},usesNativeHls:()=>false,
        watchProxyAndSwitch(file){ctx.watched.push(file.path)},loadState:async()=>{},window:{scrollTo(){}},
    };
    vm.createContext(ctx);
    for (const [a,b] of [
        ['    async function selectVideo','    async function watchProxyAndSwitch'],
        ['    function retryPreview()','    function resumePreviewRecovery'],
        ['    function editorSessionPayload()','    async function saveEditorSessionNow'],
        ['    async function addClip()', '    function queueStateUrl()'],
    ]) {
        const start=html.indexOf(a), end=html.indexOf(b,start);
        assert.ok(start>=0 && end>start);
        vm.runInContext(html.slice(start,end),ctx);
    }
    return {ctx,nodes};
}
(async()=>{
    const {ctx,nodes}=setup();
    await ctx.selectVideo({path:'new.mp4'});
    assert.equal(ctx.selectionBusy,false);
    assert.equal(ctx.spinner,false);
    assert.equal(ctx.failedSelection.file.path,'new.mp4');
    assert.equal(nodes.get('retryPreviewButton').hidden,false);
    assert.match(nodes.get('selectedFile').textContent,/25 seconds.*Retry preview/);
    assert.equal(ctx.editorSessionReady,false);
    assert.deepEqual(ctx.saved,['old.mp4']);
    assert.ok(ctx.requests.every(r=>r.options.timeoutMs===45000));
    const count=ctx.requests.length;
    await ctx.addClip();
    assert.equal(ctx.requests.length,count,'failed selection cannot add stale marks to the next video');
    ctx.mode='ready';
    ctx.retryPreview();
    for(let i=0;i<20&&ctx.selectionBusy;i++) await new Promise(setImmediate);
    assert.equal(ctx.selectionBusy,false);
    assert.equal(ctx.failedSelection,null);
    assert.equal(ctx.editorSessionReady,true);
    assert.equal(ctx.currentProxy.id,'new');
    assert.deepEqual(ctx.saved,['old.mp4'],'retry cannot save stale draft');
    assert.deepEqual(ctx.watched,['new.mp4']);
    ctx.retryPreview();assert.equal(ctx.recovery,1,'loaded player keeps normal recovery');
    ctx.selectionBusy=true;ctx.retryPreview();assert.equal(ctx.recovery,1);
    assert.match(html,/api\(queueStateUrl\(\), \{timeoutMs: 10000\}\)/);
    assert.match(html,/proxyStatusUrl\(currentProxy\), \{timeoutMs: 10000\}/);
    console.log('Preview startup: bounded waits, clear error/retry, released selection, saved drafts and normal recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1});
