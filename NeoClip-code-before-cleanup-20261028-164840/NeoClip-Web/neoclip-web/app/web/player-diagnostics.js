/* Observe native media events without changing playback or native controls. */
(() => {
    window.installNeoClipPlayerDiagnostics = (video, proxyId) => {
        const clientId = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
        const pending = [];
        let sending = false;
        let lastMouseMove = 0;
        const number = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
        const record = (event, detail = '') => {
            pending.push({
                event, at: Date.now(), media_time: number(video.currentTime),
                paused: video.paused, ready_state: video.readyState,
                network_state: video.networkState,
                hidden: document.visibilityState === 'hidden',
                proxy_id: String(proxyId() || '').slice(0, 64),
                detail: String(detail).slice(0, 160),
            });
            if (pending.length > 96) pending.shift();
        };
        const flush = async () => {
            if (sending || !pending.length) return;
            sending = true;
            const batch = pending.splice(0, 32);
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 4000);
            try {
                await fetch('/api/player/diagnostics', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({client_id: clientId, events: batch}),
                    signal: controller.signal, keepalive: true,
                });
            } catch (_) {
                // Diagnostics must never interrupt playback or trigger recovery.
            } finally {
                clearTimeout(timer);
                sending = false;
            }
        };
        for (const event of ['loadstart', 'loadedmetadata', 'durationchange', 'emptied',
                'play', 'playing', 'pause', 'waiting', 'stalled', 'seeking', 'seeked',
                'ended', 'error', 'ratechange', 'volumechange', 'resize',
                'webkitpresentationmodechanged', 'webkitbeginfullscreen', 'webkitendfullscreen']) {
            video.addEventListener(event, () => record(event,
                `rate=${video.playbackRate}; tracks=${video.videoTracks?.length ?? '?'}; ` +
                `size=${video.videoWidth}x${video.videoHeight}; error=${video.error?.code || 0}`));
        }
        for (const kind of ['videoTracks', 'audioTracks']) {
            const tracks = video[kind];
            for (const event of ['addtrack', 'removetrack', 'change']) {
                tracks?.addEventListener?.(event, () => record(`${kind}:${event}`, `count=${tracks.length}`));
            }
        }
        video.addEventListener('pointerdown', event => record('pointerdown', event.pointerType), {passive: true});
        video.addEventListener('pointermove', event => {
            if (event.pointerType === 'mouse' && Date.now() - lastMouseMove >= 1000) {
                lastMouseMove = Date.now();
                record('pointermove', 'mouse');
            }
        }, {passive: true});
        if (typeof ResizeObserver === 'function') {
            let previous = '';
            new ResizeObserver(entries => {
                const box = entries[0]?.contentRect;
                if (!box) return;
                const size = `${Math.round(box.width)}x${Math.round(box.height)}`;
                if (size !== previous) { previous = size; record('layout-resize', size); }
            }).observe(video);
        }
        if (typeof MutationObserver === 'function') {
            new MutationObserver(records => {
                for (const recordItem of records) record(`attribute:${recordItem.attributeName}`);
            }).observe(video, {attributes: true, attributeFilter: ['src', 'controls']});
        }
        document.addEventListener('visibilitychange', () => {
            record('visibilitychange');
            void flush();
        });
        window.addEventListener('pagehide', () => { record('pagehide'); void flush(); });
        setInterval(flush, 5000);
        record('diagnostics-start');
    };
})();
