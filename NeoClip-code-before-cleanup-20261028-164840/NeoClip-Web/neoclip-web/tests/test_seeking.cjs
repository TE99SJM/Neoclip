// Executes the actual shipped seek functions, not a duplicate implementation.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const html = readFileSync(`${__dirname}/../app/web/index.html`, "utf8");
const functions = html.slice(html.indexOf("    function proxyContainsTime("),
                             html.indexOf("    function jumpToTime("));
function setup() {
    const calls = {api: [], attachments: 0, loads: [], saves: 0};
    const ctx = {
        console, Date, setTimeout, clearTimeout,
        activeFile: {path: "test.mp4"}, selectionBusy: false,
        sourceDuration: 4000, proxyOffset: 3600, currentlyUsingProxy: true,
        currentProxy: {id:"same", start_seconds:3600, available_end:3800, status:"ready", playable:true},
        pendingResumeSeconds:3640, restoringPlayhead:false, proxyWatchToken:1,
        queuedSeek:null, seekWorkerActive:false, seekIntentActive:false,
        seekIntentTarget:0, seekPlaybackWanted:false, seekRevision:0, sourceSelectionEpoch:0,
        watchdogLastTime:0, watchdogStuckChecks:0,
        video: {currentTime:40, paused:true, seeking:false, readyState:4,
            seekable:{length:0}, async play(){this.paused=false}, pause(){this.paused=true},
            load(){throw Error("video.load must not run during an in-proxy seek")},
            removeAttribute(){throw Error("src must not be removed")}},
        hls: {startLoad(t){calls.loads.push(t)}},
        document: {getElementById(){return {textContent:""}}},
        formatClock: String, updateSourceClock(){}, startHeartbeat(){},
        setPlayerLoading(){}, clearPlayerLoading(){}, clearMediaLoadingIfReady(){},
        scheduleEditorSessionSave(){calls.saves++},
        sourceTime(){return ctx.proxyOffset+ctx.video.currentTime},
        async waitForMediaProgress(){},
        async api(url, options){
            calls.api.push({url,options});
            return {...ctx.currentProxy};
        },
        async watchProxyAndSwitch(){
            calls.attachments++;
            ctx.currentlyUsingProxy = true;
            ctx.video.currentTime = ctx.pendingResumeSeconds - ctx.proxyOffset;
        },
    };
    vm.createContext(ctx);
    vm.runInContext(functions, ctx);
    return {ctx,calls};
}
(async()=>{
    {
        const {ctx,calls}=setup();
        await ctx.seekSource(3670);
        assert.equal(ctx.sourceTime(),3670);
        assert.equal(ctx.currentProxy.id,"same");
        assert.equal(calls.attachments,0);
        assert.equal(calls.api.length,0);
        assert.equal(ctx.video.paused,true);
    }
    {
        const {ctx,calls}=setup();
        ctx.currentProxy.available_end=3650;
        ctx.api=async (url)=>{calls.api.push(url);return {...ctx.currentProxy,available_end:3800}};
        await ctx.seekSource(3700);
        assert.equal(ctx.sourceTime(),3700);
        assert.equal(calls.api.length,1);
        assert.equal(calls.attachments,0);
    }
    {
        const {ctx,calls}=setup();
        ctx.api=async (url)=>{
            calls.api.push(url);
            return {...ctx.currentProxy,available_end:url.includes("/start")?4000:3800};
        };
        await ctx.seekSource(3900);
        assert.equal(ctx.sourceTime(),3900);
        assert.equal(calls.api.length,2);
        assert.equal(calls.attachments,0,"same server job must never reattach");
    }
    {
        const {ctx,calls}=setup();
        ctx.api=async (url)=>{
            calls.api.push(url);
            return url.includes("/start")
                ? {...ctx.currentProxy,id:"new",start_seconds:3500,available_end:3504}
                : {...ctx.currentProxy};
        };
        await ctx.seekSource(3500);
        assert.equal(ctx.sourceTime(),3500);
        assert.equal(calls.attachments,1,"only a genuinely different window attaches");
    }
    {
        const {ctx,calls}=setup();
        ctx.video.paused=false;
        await Promise.all([ctx.skip(10),ctx.skip(10),ctx.skip(10)]);
        assert.equal(ctx.sourceTime(),3670,"rapid skips accumulate rather than reset");
        assert.equal(ctx.video.paused,false);
        assert.equal(calls.attachments,0);
        assert.equal(calls.loads.length,1,"rapid taps coalesce");
    }
    {
        const {ctx,calls}=setup();
        ctx.currentProxy.available_end=3675;
        ctx.currentProxy.status="creating";
        ctx.api=async(url)=>{
            calls.api.push(url);
            ctx.currentProxy.available_end=3684;
            return {...ctx.currentProxy};
        };
        await ctx.seekSource(3680);
        assert.equal(calls.attachments,0);
        assert.ok(!calls.api.some(url=>url.includes("/start")));
    }
    const watchdog = html.slice(html.indexOf("    function startPlaybackWatchdog("),
                                 html.indexOf("    startPlaybackWatchdog();"));
    assert.ok(!watchdog.includes("setVideoSrc("),"watchdog must not reload media on a seek");
    assert.ok(watchdog.includes("seekIntentActive"));
    console.log("7 seek regression checks passed");
})().catch(error=>{console.error(error);process.exitCode=1});
