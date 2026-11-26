const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = readFileSync(`${__dirname}/../app/web/index.html`, 'utf8');
const code = html.slice(html.indexOf('    const playbackSpeed ='),
    html.indexOf('    // Browser-owned video controls'));
assert.ok(code.includes('function applyPlaybackSpeed()'));
const mediaEvents = {}, selectEvents = {};
let rate = 1, defaultRate = 1, rateWrites = 0, defaultWrites = 0;
const video = {
    controls: true, currentTime: 29, paused: false,
    get playbackRate() { return rate; },
    set playbackRate(value) { rateWrites++; rate = value; },
    get defaultPlaybackRate() { return defaultRate; },
    set defaultPlaybackRate(value) { defaultWrites++; defaultRate = value; },
    play() { assert.fail('Speed changes must not restart playback'); },
    pause() { assert.fail('Speed changes must not pause playback'); },
    load() { assert.fail('Metadata refresh must not reload playback'); },
    addEventListener(name, fn) { mediaEvents[name] = fn; },
};
const select = {
    options: [{value:'1.0'}, {value:'1.75'}, {value:'2.0'}], value:'1.0',
    addEventListener(name, fn) { selectEvents[name] = fn; },
};
const status = {textContent: ''};
vm.runInNewContext(code, {video, document: {
    getElementById(id) { return id === 'playbackSpeed' ? select : status; },
}});
// Repeated HLS metadata notifications while playing must never send an
// otherwise redundant rate command to Safari's native player.
for (let i = 0; i < 600; i++) mediaEvents.loadedmetadata();
assert.equal(rateWrites, 0);
assert.equal(defaultWrites, 0);
assert.equal(video.currentTime, 29);
assert.equal(video.controls, true);
assert.equal(video.paused, false);
// Intentional speed changes still apply once and survive later notifications.
select.value = '1.75'; selectEvents.change();
assert.equal(rate, 1.75); assert.equal(defaultRate, 1.75);
assert.equal(rateWrites, 1); assert.equal(defaultWrites, 1);
for (let i = 0; i < 600; i++) mediaEvents.loadedmetadata();
assert.equal(rateWrites, 1); assert.equal(defaultWrites, 1);
// A genuinely reset rate on a new attachment is restored, even while paused.
rate = 1; video.paused = true;
mediaEvents.loadedmetadata();
assert.equal(rate, 1.75); assert.equal(rateWrites, 2); assert.equal(defaultWrites, 1);
assert.equal(video.paused, true);
select.value = '9.0'; selectEvents.change();
assert.equal(rateWrites, 2, 'Unsupported choices must not alter the player');
assert.match(html.match(/<video\b[^>]*>/s)[0], /\bcontrols\b/);
assert.doesNotMatch(code, /\.controls\s*=|removeAttribute\(["']controls/);
console.log('Playback speed: repeated metadata is inert; intentional changes and saved speed still work; native controls remain enabled');

// A choice made inside Safari's own speed menu becomes the saved preference.
video.readyState = 3;
rate = 2;
mediaEvents.ratechange();
const beforeNativeWrites = rateWrites;
for (let i=0;i<600;i++) mediaEvents.loadedmetadata();
assert.equal(rate,2);
assert.equal(rateWrites,beforeNativeWrites,'native choice must survive metadata without a rate command');
assert.equal(select.value,'2.0');
// Ignore the temporary reset while a new source has no decoded media yet.
video.readyState=0;rate=1;mediaEvents.ratechange();
video.readyState=3;mediaEvents.loadedmetadata();
assert.equal(rate,2);
console.log('Native speed menu preference survives metadata and attachment reset');
