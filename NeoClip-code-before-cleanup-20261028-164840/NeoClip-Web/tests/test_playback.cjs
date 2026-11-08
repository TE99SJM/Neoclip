// Run the shipped playback functions against deterministic native/HLS timelines.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const script = html.slice(html.indexOf('    const video ='), html.lastIndexOf('</script>'));
// Parse the complete script as well as executing the functions under test.
new vm.Script(script);

function setup(offset = 6) {
    const listeners = {}, elements = new Map(), calls = {loads: [], starts: [], saves: 0};
    let time = 12 - offset;
    const ctx = {
        console, Date, Math, Number, setTimeout, clearTimeout,
        activeFile: {path:'sample.mp4'}, currentSourcePath:'sample.mp4',
        selectionBusy:false, sourceDuration:12, proxyOffset:offset,
        currentProxy:{id:'end',start_seconds:offset,available_end:12,status:'ready',playable:true},
        currentlyUsingProxy:true, restoringPlayhead:false, pendingResumeSeconds:12,
        proxyWatchToken:1, queuedSeek:null, seekWorkerActive:false,
        seekIntentActive:false, seekIntentTarget:0, seekPlaybackWanted:false,
        seekRevision:0, sourceSelectionEpoch:0, playWhenProxyReady:false,
        watchdogLastTime:0, watchdogStuckChecks:0, markInSeconds:2, markOutSeconds:4,
        document:{getElementById(id){
            if (!elements.has(id)) elements.set(id,{textContent:'',value:''});
            return elements.get(id);
        }},
        hls:{startLoad(t){calls.loads.push(t)}},
        formatClock:String, updateSourceClock(){}, startHeartbeat(){}, stopHeartbeat(){},
        destroyHls(){}, setPlayerLoading(){}, clearPlayerLoading(){}, clearMediaLoadingIfReady(){},
        scheduleEditorSessionSave(){calls.saves++},
        optionalSessionTime:value=>value,
        sourceTime(){return ctx.proxyOffset+ctx.video.currentTime},
        async waitForMediaProgress(){},
        async api(url, options){
            if (!url.includes('/start')) return {...ctx.currentProxy};
            const target=JSON.parse(options.body).current_time;
            calls.starts.push(target);
            return {id:'beginning',start_seconds:target,available_end:12,status:'ready',playable:true};
        },
        async watchProxyAndSwitch(){
            ctx.currentlyUsingProxy=true;
            await ctx.seekVideoToSavedTime(ctx.pendingResumeSeconds);
            if (ctx.playWhenProxyReady) await ctx.video.play();
        },
    };
    const emit = name => (listeners[name] || []).forEach(fn=>fn());
    ctx.video = {
        paused:true, ended:true, seeking:false, readyState:4,
        // AAC priming can put the first video frame 23 ms after zero.
        seekable:{length:1,start(){return .023},end(){return 12-ctx.proxyOffset}},
        get currentTime(){return time},
        set currentTime(value){time=Math.max(.023,value);this.ended=false;emit('seeked')},
        async play(){this.paused=false;if(this.ended)this.currentTime=0;emit('play')},
        pause(){this.paused=true}, load(){}, removeAttribute(){},
        addEventListener(name,fn){(listeners[name] ||= []).push(fn)},
    };
    vm.createContext(ctx);
    const include=(start,end)=>vm.runInContext(html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start))),ctx);
    include('    async function seekVideoToSavedTime(', '    async function api(');
    include('    function editorSessionPayload()', '    async function saveEditorSessionNow()');
    include('    let playbackEnded =', '    function updatePictureInPictureButton()');
    include(html.includes('    function proxyContainsTime(') ? '    function proxyContainsTime(' : '    async function seekSource(', '    function jumpToTime(');
    return {ctx,calls,emit,elements};
}

(async()=>{
    {
        const {ctx,calls,emit,elements}=setup();
        emit('ended');
        assert.equal(elements.get('playPauseButton').textContent,'Replay');
        const session=ctx.editorSessionPayload();
        assert.equal(session.current_time,0);
        assert.equal(session.mark_in,2);
        assert.equal(session.mark_out,4);
        await ctx.togglePlayback();
        assert.deepEqual(calls.starts,[0], 'Replay must leave the end-only window');
        assert.ok(ctx.sourceTime()<.25);
        assert.equal(ctx.video.paused,false);
        assert.equal(elements.get('playPauseButton').textContent,'Play/Pause');
        ctx.video.pause();
        await ctx.seekSource(1);
        assert.ok(Math.abs(ctx.sourceTime()-1)<.05);
        assert.equal(ctx.video.paused,true,'ordinary seeks preserve pause');
        await ctx.seekSource(0);
        assert.ok(ctx.sourceTime()<.25,'zero lands on the first video frame');
        assert.ok(calls.loads.includes(0) || calls.loads.includes(.023));
        assert.ok(!calls.loads.includes(-1),'zero must not mean auto/live edge');
    }
    {
        const {ctx,calls,emit}=setup();
        emit('ended');
        await ctx.video.play(); // native controls / PiP replay
        const deadline=Date.now()+3000;
        while (Date.now()<deadline && ctx.proxyOffset!==0) await new Promise(r=>setTimeout(r,10));
        assert.deepEqual(calls.starts,[0]);
        assert.equal(ctx.proxyOffset,0);
        assert.equal(ctx.video.paused,false);
    }
    {
        const {ctx,emit}=setup(0);
        emit('ended');
        ctx.video.currentTime=3; // native scrub after finishing, while paused
        assert.equal(ctx.editorSessionPayload().current_time,3);
    }
    console.log('Replay, native replay, EOF bookmark, first-frame and one-second checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
