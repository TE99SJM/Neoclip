/* Temporary, read-only diagnostics. Enabled by the server's environment only. */
(() => {
    window.installNeoClipTestDiagnostics = ({video, getProxy}) => {
        if (document.documentElement.dataset.testDiagnostics !== 'true') return null;
        if (document.getElementById('testDiagnostics')) return null;
        const main = document.querySelector('main');
        if (!main || !video) return null;

        const element = (tag, text, parent) => {
            const node = document.createElement(tag);
            if (text) node.textContent = text;
            if (parent) parent.appendChild(node);
            return node;
        };
        const panel = element('section');
        panel.id = 'testDiagnostics';
        panel.className = 'test-diagnostics';
        panel.setAttribute('aria-label', 'Temporary test diagnostics');
        element('strong', 'Test diagnostics', panel);
        const badges = element('div', '', panel);
        badges.className = 'test-diagnostic-badges';
        const playerBadge = element('span', '', badges);
        const proxyBadge = element('span', '', badges);
        const errorBadge = element('span', '', badges);
        const connectionBadge = element('span', '', badges);
        const details = element('details', '', panel);
        const summary = element('summary', 'Recent events (0)', details);
        const log = element('textarea', '', details);
        log.readOnly = true;
        log.rows = 8;
        log.setAttribute('aria-label', 'Recent diagnostic events');
        const actions = element('div', '', details);
        actions.className = 'test-diagnostic-actions';
        const copy = element('button', 'Copy log', actions);
        const clear = element('button', 'Clear log', actions);
        copy.type = clear.type = 'button';
        const notice = element('span', 'Temporary: resets when this page reloads.', details);
        notice.setAttribute('role', 'status');
        main.insertBefore(panel, main.firstChild);

        const entries = [];
        const listeners = [];
        const previousMessages = new Map();
        let errors = 0;
        let lastProxy = '';
        let lastProxyError = '';
        let disposed = false;
        let timer = null;
        const text = value => String(value ?? '').replace(/[\r\n]+/g, ' ').slice(0, 240);
        const clock = () => new Date().toLocaleTimeString();
        const isError = message => /\berror\b|failed|unavailable|timed out|could not|cannot/i.test(message);
        const show = () => {
            log.value = entries.join('\n');
            summary.textContent = `Recent events (${entries.length})`;
            errorBadge.textContent = `Errors: ${errors}`;
            errorBadge.dataset.error = errors ? 'true' : 'false';
        };
        const record = (kind, message, error = false) => {
            if (disposed) return;
            if (error) errors++;
            const time = Number(video.currentTime);
            const position = Number.isFinite(time) ? ` @ ${time.toFixed(1)}s` : '';
            entries.push(`${clock()} [${text(kind)}]${position} ${text(message)}`);
            if (entries.length > 100) entries.shift();
            show();
        };
        const listen = (target, event, callback) => {
            target.addEventListener(event, callback);
            listeners.push(() => target.removeEventListener(event, callback));
        };
        const playerState = () => video.error ? `Error ${video.error.code}` :
            video.ended ? 'Ended' : video.seeking ? 'Seeking' :
            !video.currentSrc ? 'Idle' : video.paused ? 'Paused' :
            video.readyState < 3 ? 'Buffering' : 'Playing';
        const update = () => {
            if (disposed) return;
            playerBadge.textContent = `Player: ${playerState()}`;
            connectionBadge.textContent = navigator.onLine === false ? 'Offline' : 'Online';
            const proxy = getProxy();
            const progress = Math.min(100, Math.max(0, Math.round(Number(proxy?.progress) || 0)));
            const state = text(proxy?.build_status || proxy?.status || 'Not selected');
            proxyBadge.textContent = `Proxy: ${state}${proxy ? ` ${progress}%` : ''}`;
            const signature = JSON.stringify([proxy?.id, proxy?.generation, state, progress]);
            if (signature !== lastProxy) {
                lastProxy = signature;
                if (proxy) record('Proxy', `${state} ${progress}%${proxy.pipeline ? `; ${proxy.pipeline}` : ''}`);
            }
            const proxyError = text(proxy?.error);
            if (proxyError && proxyError !== lastProxyError) record('Proxy error', proxyError, true);
            lastProxyError = proxyError;
            for (const [id, label] of [['selectedFile', 'Preview'], ['pipelineStatus', 'Pipeline'],
                    ['renderStatus', 'Render'], ['fileStatus', 'Library'], ['speedStatus', 'Speed']]) {
                const message = text(document.getElementById(id)?.textContent?.trim());
                if (previousMessages.get(id) === message) continue;
                previousMessages.set(id, message);
                if (message) record(label, message, isError(message));
            }
        };
        for (const name of ['loadstart', 'loadedmetadata', 'playing', 'pause', 'waiting',
                'stalled', 'seeking', 'seeked', 'ended', 'error', 'ratechange']) {
            listen(video, name, () => {
                record(name === 'error' ? 'Player error' : 'Player',
                    `${name}; speed=${video.playbackRate}; ready=${video.readyState}` +
                    (video.error ? `; code=${video.error.code}; ${video.error.message || ''}` : ''),
                    name === 'error');
                update();
            });
        }
        listen(window, 'error', event => record('Script error', event.message || 'Script failed', true));
        listen(window, 'unhandledrejection', event => record('Request error',
            event.reason?.message || event.reason || 'Unhandled promise rejection', true));
        for (const name of ['online', 'offline']) listen(window, name, () => {
            record('Connection', name); update();
        });
        const start = () => {
            if (timer === null && document.visibilityState !== 'hidden') timer = setInterval(update, 1000);
        };
        const stop = () => { if (timer !== null) clearInterval(timer); timer = null; };
        listen(document, 'visibilitychange', () => {
            record('App', document.visibilityState);
            if (document.visibilityState === 'hidden') stop();
            else { update(); start(); }
        });
        listen(window, 'pagehide', () => { record('App', 'Page hidden'); stop(); });
        listen(window, 'pageshow', () => { update(); start(); });
        listen(clear, 'click', () => {
            entries.length = 0; errors = 0; show();
            notice.textContent = 'Log cleared.';
        });
        listen(copy, 'click', async () => {
            const version = document.querySelector('.version')?.textContent || 'NeoClip test';
            const output = `${version}\n${entries.join('\n')}`;
            try {
                if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
                await navigator.clipboard.writeText(output);
                notice.textContent = 'Log copied.';
            } catch (_) {
                // HTTP LAN/Tailscale pages may lack clipboard permission.
                log.focus(); log.select();
                notice.textContent = 'Log selected. Use your device’s Copy command.';
            }
        });
        record('App', 'Temporary diagnostics enabled');
        update(); start();
        return {
            dispose() {
                disposed = true; stop();
                listeners.forEach(remove => remove());
                panel.remove();
            },
        };
    };
})();
