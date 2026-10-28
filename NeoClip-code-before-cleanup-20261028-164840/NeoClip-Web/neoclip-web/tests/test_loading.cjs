// Exercise the shipped loading and page-return functions with deterministic media state.
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const html = readFileSync(`${__dirname}/../app/web/index.html`, "utf8");
const elements = new Map();
const listeners = {};
const ctx = {
    Map, String, Number, setTimeout, clearTimeout,
    document: {
        visibilityState: "visible",
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, {
                textContent: "", hidden: true, attrs: {},
                classList: {toggle(name, hidden) {elements.get(id).hidden = hidden;}},
                setAttribute(name, value) {this.attrs[name] = value;},
            });
            return elements.get(id);
        },
    },
    video: {paused: true, seeking: false, readyState: 0, ended: false, error: null,
        currentTime: 40, addEventListener(name, callback) {
            (listeners[name] ||= []).push(callback);
        }},
};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf("    const loadingReasons"),
                           html.indexOf("    let hls =")), ctx);
const event = name => (listeners[name] || []).forEach(fn => fn());
const visible = () => !elements.get("playerLoading").hidden;
const text = () => elements.get("playerLoadingText").textContent;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
    ctx.setPlayerLoading("proxy", "Preparing preview…");
    assert.equal(visible(), true);
    assert.equal(elements.get("videoStage").attrs["aria-busy"], "true");
    ctx.video.readyState = 4;
    event("canplay");
    assert.equal(visible(), true, "canplay must not hide an active app operation");
    ctx.setPlayerLoading("seek", "Seeking to 1:00…");
    assert.equal(text(), "Seeking to 1:00…");
    ctx.clearPlayerLoading("seek");
    assert.equal(text(), "Preparing preview…");
    ctx.clearPlayerLoading("proxy");
    assert.equal(visible(), false);
    ctx.video.paused = false;
    ctx.video.readyState = 1;
    event("waiting");
    await sleep(180);
    assert.equal(text(), "Buffering…");
    ctx.video.readyState = 4;
    event("playing");
    assert.equal(visible(), false);
    ctx.video.paused = true;
    ctx.video.readyState = 1;
    event("waiting");
    await sleep(180);
    assert.equal(visible(), false, "intentional pause has no phantom spinner");
    ctx.setPlayerLoading("proxy", "Preparing preview…");
    event("error");
    assert.equal(visible(), false);
    assert.match(elements.get("selectedFile").textContent, /could not be loaded/);
    let heartbeats = 0;
    const loads = [];
    Object.assign(ctx, {
        currentSourcePath: "sample.mp4", currentlyUsingProxy: true,
        restoringPlayhead: false, seekIntentActive: false,
        userWantsPlayback: true, startHeartbeat(){heartbeats++;},
        hls: {startLoad(t){loads.push(t);}},
    });
    vm.runInContext(html.slice(html.indexOf("    function resumeVisiblePlayer("),
                               html.indexOf('    window.addEventListener("pageshow"')), ctx);
    ctx.resumeVisiblePlayer();
    assert.equal(heartbeats, 1);
    assert.deepEqual(loads, [40]);
    ctx.userWantsPlayback = false;
    ctx.resumeVisiblePlayer();
    assert.deepEqual(loads, [40], "returning must respect intentional pause");
    ctx.document.visibilityState = "hidden";
    ctx.resumeVisiblePlayer();
    assert.equal(heartbeats, 2);
    console.log("Loading state and page-return regression checks passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
