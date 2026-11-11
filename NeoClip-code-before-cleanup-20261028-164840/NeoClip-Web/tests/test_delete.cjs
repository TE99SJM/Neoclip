const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const code = html.slice(html.indexOf('    async function deleteVideo()'), html.indexOf('    async function selectVideo'));
async function exercise(fail) {
    const events = [], elements = new Map();
    const saved = {path:'selected.mp4',current_time:90,mark_in:2,mark_out:4,clip_name:'Keep me'};
    let release;
    const ctx = {
        currentSourcePath:'selected.mp4',deletionBusy:false,selectionBusy:false,
        pendingResumeSeconds:0,proxyWatchToken:1,currentlyUsingProxy:true,currentProxy:{id:1},
        sessionSaveTimer:1,markInSeconds:2,markOutSeconds:4,lastSavedSessionJson:'saved',activeFile:{path:'selected.mp4'},
        window:{confirm:()=>true},editorSessionPayload:()=>saved,
        cancelPendingSeeks(){},resetPlayerLoading(){},stopHeartbeat(){},clearTimeout(){},
        destroyHls(){events.push('destroy')},
        video:{pause(){events.push('pause')},removeAttribute(name){assert.equal(name,'src');events.push('detach')},load(){events.push('load')}},
        document:{getElementById(id){if(!elements.has(id)) elements.set(id,{textContent:'',classList:{add(){}}});return elements.get(id)}},
        applyEditorSession(session){assert.deepEqual(session,saved);events.push('restore')},
        updateMarkStatus(){},alert(message){assert.match(message,/Delete failed/);events.push('alert')},
        async loadState(){events.push('state')},async loadFiles(){events.push('files')},
        async api(url, options){
            assert.equal(url,'/api/media/delete');
            assert.deepEqual(events,['destroy','pause','detach','load']);
            assert.equal(ctx.currentSourcePath,null);
            assert.equal(JSON.parse(options.body).path,'selected.mp4');
            events.push('delete');
            await new Promise(resolve=>{release=resolve});
            if(fail) throw new Error('NAS busy');
            return {ok:true,exists:false};
        },
    };
    vm.createContext(ctx);vm.runInContext(code,ctx);
    const task=ctx.deleteVideo();
    assert.equal(ctx.deletionBusy,true);
    await ctx.deleteVideo(); // A second click does not send another delete.
    release();await task;
    assert.equal(ctx.deletionBusy,false);
    if(fail){
        assert.equal(ctx.currentSourcePath,'selected.mp4');
        assert.equal(ctx.markInSeconds,2);assert.equal(ctx.markOutSeconds,4);
        assert.equal(ctx.pendingResumeSeconds,90);
        assert.equal(ctx.currentProxy,null,'failure does not reopen the NAS file');
        assert.deepEqual(events.slice(-2),['restore','alert']);
    }else{
        assert.equal(ctx.currentSourcePath,null);assert.equal(ctx.activeFile,null);
        assert.equal(ctx.markInSeconds,null);assert.deepEqual(events.slice(-2),['state','files']);
    }
}
(async()=>{await exercise(false);await exercise(true);console.log('delete player release and failed-delete edit retention passed')})().catch(error=>{console.error(error);process.exitCode=1});
