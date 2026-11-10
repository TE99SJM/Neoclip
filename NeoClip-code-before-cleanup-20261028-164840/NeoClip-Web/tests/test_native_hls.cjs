// Execute the shipped media loader and verify Safari keeps browser controls.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const functions = html.slice(html.indexOf('    function usesNativeHls('),
                            html.indexOf('    let markInSeconds'));
assert.match(html, /<video[^>]+id="video"[^>]+\bcontrols\b/);
if (__dirname.includes('/neoclip-web/')) {
    assert.ok(!html.includes('id="sourceTimeline"'), 'No extra timeline in the current UI');
}

(async () => {
    for (const [agent, native] of [
        ['Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1', true],
        ['Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15', true],
        ['Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 Chrome/130.0 Safari/537.36', false],
    ]) {
        const events = new Map();
        let attachments = 0;
        class Hls {
            static isSupported() { return true; }
            static Events = {MANIFEST_PARSED: 'manifest', ERROR: 'error'};
            loadSource(url) { this.url = url; }
            attachMedia() { attachments++; }
            on(name, callback) { if (name === 'manifest') callback(); }
        }
        const ctx = {
            navigator: {userAgent: agent}, window: {Hls}, Hls,
            pendingResumeSeconds: 0, proxyOffset: 0,
            hls: null, destroyHls() {}, setTimeout, clearTimeout,
            video: {
                readyState: 1, canPlayType() { return 'probably'; },
                addEventListener(name, fn) { events.set(name, fn); },
                removeEventListener(name) { events.delete(name); },
                load() { events.get('loadedmetadata')(); },
            },
        };
        vm.createContext(ctx);
        vm.runInContext(functions, ctx);
        assert.equal(ctx.usesNativeHls(), native);
        assert.equal(ctx.proxyStatusUrl({id:'one',status_url:'/api/vod/status?id=one'}), '/api/vod/status?id=one');
        assert.equal(ctx.proxyStatusUrl({id:'old'}), '/api/proxy/status?id=old');
        await ctx.setVideoSrc('/api/vod/playlist.m3u8?id=one');
        assert.equal(attachments, native ? 0 : 1);
        if (native) assert.equal(ctx.video.src, '/api/vod/playlist.m3u8?id=one');
        assert.equal(events.size, 0, 'Loader listeners are removed once ready');
    }
    console.log('Safari native VOD routing, media loading and controls checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
