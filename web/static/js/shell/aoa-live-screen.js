/**
 * aoa-live-screen —— AOA 录制器 scrcpy 实时投屏（WebCodecs 解码）。
 *
 * 数据通道：GET 升级 WS `/api/devices/aoa/record/stream/{serial}`：
 *   - 首帧 JSON：{type:"hello", codec, frames}
 *   - 后续二进制：scrcpy 视频字节流，12B 帧头 [时间戳(8,忽略) 长度(4 BE)]
 *     + Annex-B H.264 负载；帧类型由 NAL 自判（IDR(5)=关键帧，
 *     仅 SPS(7)/PPS(8)=配置帧）。
 *
 * 约束：投屏只做显示器。所有输入（点按/滑动/按键）必须走
 * `/api/devices/aoa/record/input` 生成 AOA 命令，本模块不注入任何
 * 设备事件（scrcpy-server 以 control=false 启动，无控制通道）。
 *
 * 兜底：浏览器无 WebCodecs 或解码失败时回调 fallback，由调用方切回
 * 截图轮询通道（截图合并刷新）。
 */
(function () {
    'use strict';

    const state = {
        active: false,      // 期望状态（用户开启）
        ws: null,
        decoder: null,
        canvas: null,
        buffer: new Uint8Array(0),
        pendingConfig: null,
        framesDecoded: 0,
        lastPongAt: 0,      // 最近一次服务端 pong 时间（连接判活）
        retried: false,     // 有界自动重连：每次手动开启后只允许一次
        failing: false,     // 失败处理闩（onerror/onclose 双触发去重）
        lastTarget: null,   // 最近一次开启的 {serial, workerId}
        generation: 0,      // 会话代次：stop/新 start 递增；过期回调与
                            // 迟到事件按代次丢弃，防止 A 的重连回调覆盖
                            // B 的会话（审核 P1 竞态）
        reconnectTimer: null,
        streamTarget: null, // 已成功建立服务端会话的 {serial, workerId}
        pollTimer: null,
        pingTimer: null,
        watchdogTimer: null,
        statusTimer: null,
        timestampUs: 0,
        stopWs: false,
    };

    function el(id) { return document.getElementById(id); }

    // 开启新会话前调用：代次 +1 使所有旧回调/迟到事件过期，并取消挂起
    // 的自动重连定时器。
    function bumpGeneration() {
        state.generation += 1;
        if (state.reconnectTimer) {
            clearTimeout(state.reconnectTimer);
            state.reconnectTimer = null;
        }
    }

    // 主动停止时释放服务端 scrcpy 会话（审核 P1）：否则编码器/ADB 转发
    // 要等 180 秒空闲清理，4 个会话额度可能被快速切换耗尽。fire-and-
    // forget：网络失败交给服务端空闲回收兜底（浏览器崩溃/断网同路径）。
    // 代次防护：stop 捕获当前代次，若此后已有更新的会话开启（同步代码
    // 顺序保证检查先于任何新 start），不再发送过期 stop。
    function releaseServerStream(target) {
        if (!target) return;
        const generation = state.generation;
        fetch(
            `/api/devices/aoa/record/stream/${encodeURIComponent(target.serial)}/stop`,
            {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({worker_id: target.workerId, generation})}
        ).catch(() => { /* 服务端 180s 空闲清理兜底 */ });
    }

    function wsUrl(serial) {
        const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
        return `${scheme}${location.host}/api/devices/aoa/record/stream/${encodeURIComponent(serial)}`;
    }

    // ---- Annex-B NAL 解析 ----

    function findStartCode(bytes, from) {
        for (let i = from; i + 3 <= bytes.length; i += 1) {
            if (bytes[i] === 0 && bytes[i + 1] === 0) {
                if (bytes[i + 2] === 1) return [i, 3];
                if (i + 4 <= bytes.length && bytes[i + 3] === 1) return [i, 4];
            }
        }
        return null;
    }

    function extractParameterSets(configBytes) {
        // config 帧负载：Annex-B 的 SPS(7)/PPS(8)。
        const found = { sps: null, pps: null };
        let cursor = 0;
        while (cursor < configBytes.length) {
            const head = findStartCode(configBytes, cursor);
            if (!head) break;
            const nalStart = head[0] + head[1];
            const next = findStartCode(configBytes, nalStart);
            const nalEnd = next ? next[0] : configBytes.length;
            const type = configBytes[nalStart] & 0x1F;
            const unit = configBytes.slice(nalStart, nalEnd);
            if (type === 7 && !found.sps) found.sps = unit;
            if (type === 8 && !found.pps) found.pps = unit;
            cursor = nalEnd > cursor ? nalEnd : cursor + 1;
        }
        return found;
    }

    // ---- 解码与渲染 ----

    function configureDecoder(sps) {
        const profile = sps[1].toString(16).padStart(2, '0');
        const compat = sps[2].toString(16).padStart(2, '0');
        const level = sps[3].toString(16).padStart(2, '0');
        const decoder = new VideoDecoder({
            output(frame) {
                if (state.decoder !== decoder) { frame.close(); return; }
                // 每帧实时查 DOM（state.canvas 从未赋值导致解码帧被丢弃、
                // 计数不涨、看门狗误回退——8 秒未出画面的真根因）。
                const canvas = el('aoa-screen-canvas');
                if (canvas && !canvas.hidden) {
                    if (canvas.width !== frame.displayWidth) canvas.width = frame.displayWidth;
                    if (canvas.height !== frame.displayHeight) canvas.height = frame.displayHeight;
                    canvas.getContext('2d').drawImage(frame, 0, 0);
                    canvas.dataset.aoaFrameReady = '1';
                }
                // 解码成功即计数（与绘制解耦）：看门狗据此判断流健康。
                state.framesDecoded += 1;
                frame.close();
            },
            error() { handleStreamFailure('解码器错误，已回退截图模式'); },
        });
        decoder.configure({
            codec: `avc1.${profile}${compat}${level}`,
            optimizeForLatency: true,
        });
        state.decoder = decoder;
    }

    function feedFrame(pts, payload) {
        // flags 字节已取消（12B 头）：从 NAL 类型自判——含 IDR(5)=关键帧，
        // 仅 SPS(7)/PPS(8) 等参数集=配置帧。
        let isConfig = false;
        let isKey = false;
        let cursor = 0;
        while (cursor < payload.length) {
            const head = findStartCode(payload, cursor);
            if (!head) break;
            const nalStart = head[0] + head[1];
            const type = payload[nalStart] & 0x1F;
            if (type >= 1 && type <= 5) isKey = isKey || type === 5;
            else if (type === 7 || type === 8) isConfig = true;
            const next = findStartCode(payload, nalStart);
            cursor = next ? next[0] : payload.length;
        }
        // 服务端重放的关键帧内联 SPS/PPS（不是独立 config 帧）：解码器
        // 未初始化时必须就地从该帧配置——否则整帧被丢弃、后续 delta 帧
        // 永远无法解码（实机复现：8 秒看门狗回退的根因）。
        if (!state.decoder && isConfig) {
            const sets = extractParameterSets(payload);
            if (sets.sps && sets.pps) {
                try {
                    configureDecoder(sets.sps);
                } catch (error) {
                    handleStreamFailure(`解码器配置失败（${error.message}），已回退截图模式`);
                    return;
                }
            }
        }
        if (!state.decoder || state.decoder.state !== 'configured') return;
        let data = payload;
        if (state.pendingConfig && !isConfig) {
            // pendingConfig 是原始 Annex-B 负载（含起始码），与帧负载拼接
            // 后仍是合法 Annex-B。
            const merged = new Uint8Array(state.pendingConfig.length + payload.length);
            merged.set(state.pendingConfig, 0);
            merged.set(payload, state.pendingConfig.length);
            data = merged;
            state.pendingConfig = null;
        }
        if (!data.length) return;
        state.timestampUs += 33333;
        try {
            state.decoder.decode(new EncodedVideoChunk({
                type: isKey ? 'key' : 'delta',
                timestamp: state.timestampUs,
                data,
            }));
        } catch (error) {
            handleStreamFailure(`解码失败（${error.message}），已回退截图模式`);
        }
    }

    function consumeBuffer() {
        let bytes = state.buffer;
        for (;;) {
            if (bytes.length < 12) break;
            const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
            // 12B 帧头：pts(8，实机观测 config 帧为 AV_NOPTS 标记，忽略，
            // WebCodecs 时间戳改用单调自增) + length(4, BE)。
            const pts = view.getBigUint64(0);
            const length = view.getUint32(8);
            if (bytes.length < 12 + length) break;
            const payload = bytes.slice(12, 12 + length);
            bytes = bytes.slice(12 + length);
            feedFrame(pts, payload);
        }
        state.buffer = bytes;
    }

    // ---- 会话控制 ----

    function setStatus(text, isError) {
        const node = el('aoa-screen-status');
        if (node) {
            node.textContent = text;
            node.style.color = isError ? '#c62828' : '';
        }
    }

    function showCanvas() {
        const canvas = el('aoa-screen-canvas');
        const image = el('aoa-screen-image');
        const empty = el('aoa-screen-empty');
        if (canvas) canvas.hidden = false;
        if (image) image.hidden = true;
        if (empty) empty.hidden = true;
    }

    function startScreenshotPolling() {
        state.active = false;
        const canvas = el('aoa-screen-canvas');
        if (canvas) canvas.hidden = true;
        stopScreenshotPolling();
        if (typeof window.aoaRefreshScreen !== 'function') return;
        window.aoaRefreshScreen();
        // 不依赖 state.active：实时流启动失败的回退路径里 active 已复位，
        // 但画面仍需轮询（stop() 统一负责停止）。
        state.pollTimer = setInterval(() => {
            if (typeof window.aoaRefreshScreen !== 'function') return;
            window.aoaRefreshScreen();
        }, 1200);
    }

    function stopScreenshotPolling() {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    }

    function handleStreamFailure(message) {
        if (!state.active || state.failing) return;
        state.failing = true;   // onerror/onclose 可能双触发，只走一次
        teardownStream();
        // 失败诊断（审核要求）：E2E 失败时需要能对出启动状态、代次与帧
        // 计数，否则无法区分启动请求失败、清单刷新误 stop 还是流竞态。
        console.debug('[aoa-live-screen] stream failure:', {
            message, generation: state.generation,
            serial: state.lastTarget?.serial, framesDecoded: state.framesDecoded,
            retried: state.retried,
        });
        // 有界自动重连：USB 重枚举（实机观测：移除→约 4 秒后加回）或 adb
        // 瞬断会杀掉 scrcpy 会话，这类故障几秒内自愈。失败后自动重试一次；
        // 重试再失败才落回截图模式（不循环重试，服务下线时不会刷请求）。
        // 用户手动重新点击「▶ 实时投屏」会复位重试预算（start 非 retry）。
        if (!state.retried && state.lastTarget) {
            const {serial, workerId} = state.lastTarget;
            state.retried = true;
            setStatus(`${message || '实时流断开'}，3 秒后自动重连…`, true);
            // 代次防护（审核 P1）：回调触发时若用户已停止/切换设备
            // （generation 变化），立即丢弃，不得覆盖新会话。
            const generation = state.generation;
            state.reconnectTimer = setTimeout(() => {
                state.reconnectTimer = null;
                if (state.generation !== generation || !state.active || state.ws) return;
                state.failing = false;
                state.active = false;   // 让 start() 的 active 守卫放行重启
                start(serial, workerId, {isRetry: true});
            }, 3000);
            return;
        }
        state.failing = false;
        setStatus(message || '实时投屏不可用，已回退截图模式', true);
        startScreenshotPolling();
        // 回退后按钮必须回到"▶ 实时投屏"（停止态），与 aoaSyncLiveButton
        // 保持同一状态语义；直接改 DOM，避免跨模块依赖。
        const button = el('aoa-toggle-live');
        if (button) {
            button.setAttribute('data-live', 'off');
            button.textContent = '▶ 实时投屏';
            button.title = '点击开始实时投屏';
        }
    }

    function teardownStream() {
        const canvas = el('aoa-screen-canvas');
        if (canvas) delete canvas.dataset.aoaFrameReady;
        state.stopWs = true;
        if (state.pingTimer) { clearInterval(state.pingTimer); state.pingTimer = null; }
        if (state.watchdogTimer) { clearInterval(state.watchdogTimer); state.watchdogTimer = null; }
        if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
        if (state.ws) {
            try { state.ws.close(); } catch (error) { /* already closing */ }
            state.ws = null;
        }
        if (state.decoder) {
            try { state.decoder.close(); } catch (error) { /* not initialized */ }
            state.decoder = null;
        }
        state.buffer = new Uint8Array(0);
        state.pendingConfig = null;
    }

    async function start(serial, workerId, {isRetry = false} = {}) {
        if (state.active) return;
        if (!isRetry) state.retried = false;   // 用户手动开启：复位重试预算
        // 新会话开启：递增代次使旧重连回调/迟到事件全部过期（含 HTTP
        // await 期间被 stop()/切换设备废弃的场景）。
        bumpGeneration();
        const generation = state.generation;
        const target = {serial, workerId};
        state.lastTarget = target;
        state.failing = false;
        if (typeof VideoDecoder === 'undefined') {
            state.active = true;
            setStatus('浏览器不支持 WebCodecs，使用截图模式（约1秒刷新）', false);
            startScreenshotPolling();
            return;
        }
        state.active = true;
        stopScreenshotPolling();
        setStatus('正在建立实时投屏…');
        let hello;
        try {
            const response = await fetch(
                `/api/devices/aoa/record/stream/${encodeURIComponent(serial)}/start`,
                {method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({worker_id: workerId})});
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || payload.success === false) {
                throw new Error(payload.message || payload.detail || `HTTP ${response.status}`);
            }
            hello = payload;
        } catch (error) {
            if (state.generation !== generation) return;   // 已被 stop/切换废弃
            console.debug('[aoa-live-screen] start failed:', {
                message: error.message, generation, serial,
            });
            state.active = false;
            setStatus(`实时投屏启动失败：${error.message}`, true);
            startScreenshotPolling();
            return;
        }
        // HTTP await 期间被 stop()/切换设备废弃：服务端会话刚建立但本次
        // 连接已过期——不同设备才补发 stop（同设备的新会话已接管服务端
        // 会话，stop 会误杀）；同设备的残留会话由空闲清理兜底。
        if (state.generation !== generation) {
            const current = state.lastTarget;
            if (!current || current.serial !== serial) releaseServerStream(target);
            return;
        }
        state.streamTarget = target;
        state.stopWs = false;
        const socket = new WebSocket(wsUrl(serial));
        socket.binaryType = 'arraybuffer';
        state.ws = socket;
        const socketAlive = () => state.generation === generation && state.ws === socket;
        socket.onmessage = (event) => {
            if (!socketAlive()) return;
            if (typeof event.data === 'string') {
                // 服务端 pong（JSON 文本）：记录接收时间，作为连接判活依据
                // （静态画面长时间无新帧是正常现象，不能当流死亡）。
                try {
                    if (JSON.parse(event.data)?.type === 'pong') state.lastPongAt = Date.now();
                } catch (_) { /* 非 pong 文本，忽略 */ }
                return;
            }
            state.buffer = concat(state.buffer, new Uint8Array(event.data));
            consumeBuffer();
        };
        socket.onerror = () => {
            if (!socketAlive()) return;
            handleStreamFailure('实时流连接失败，已回退截图模式');
        };
        socket.onclose = () => {
            // 过期会话的 close（stop/切换/重连替换）不触发失败处理。
            if (socketAlive() && !state.stopWs) handleStreamFailure('实时流已断开，已回退截图模式');
        };
        socket.onopen = () => {
            if (!socketAlive()) { try { socket.close(); } catch (_) { /* closing */ } return; }
            showCanvas();
            setStatus('实时投屏中 · 等待首帧…');
            // 判活只看 ping/pong 心跳（15 秒无 pong ≈ 半开连接），绝不因
            // "无新帧"杀流：设备熄屏/无画面输出时编码器本就不出帧，心跳
            // 正常说明链路活着，亮屏后画面自动到来（实机复现：熄屏约 8 秒
            // 后被首帧超时误杀断开）。会话真死时服务端会关闭 WS（onclose
            // 兜底），中继死亡时 pump 的 finally 也会主动关闭订阅者 WS。
            state.lastPongAt = Date.now();
            state.watchdogTimer = setInterval(() => {
                if (!state.active) { clearInterval(state.watchdogTimer); return; }
                if (Date.now() - state.lastPongAt >= 15000) {
                    clearInterval(state.watchdogTimer);
                    state.watchdogTimer = null;
                    handleStreamFailure('实时流心跳超时，已回退截图模式');
                }
            }, 4000);
            // 心跳 + 帧率指示（用户可直观确认画面在实时更新）。
            let lastShown = -1;
            state.statusTimer = setInterval(() => {
                if (!state.active) return;
                if (state.framesDecoded === 0) {
                    setStatus('实时投屏中 · 等待设备画面（熄屏时无帧属正常，可点 Wake 唤醒）');
                    return;
                }
                if (state.framesDecoded !== lastShown) {
                    lastShown = state.framesDecoded;
                    setStatus(`实时投屏中 · 已解码 ${state.framesDecoded} 帧 · 单击/拖动发送操作`);
                }
            }, 2000);
            state.pingTimer = setInterval(() => {
                if (socket.readyState === WebSocket.OPEN) socket.send('ping');
            }, 5000);
        };
    }

    function concat(a, b) {
        const merged = new Uint8Array(a.length + b.length);
        merged.set(a, 0);
        merged.set(b, a.length);
        return merged;
    }

    function stop() {
        // 先递增代次：所有挂起重连回调与迟到 WS 事件立即过期。
        bumpGeneration();
        const hadServerSession = state.streamTarget;
        state.streamTarget = null;
        state.active = false;
        teardownStream();
        stopScreenshotPolling();
        // 用户主动停止：立即释放服务端 scrcpy 会话（不等 180s 空闲清理）。
        releaseServerStream(hadServerSession);
        const canvas = el('aoa-screen-canvas');
        if (canvas) { canvas.hidden = true; canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height); }
    }

    function activeSurface() {
        // 投屏激活且 canvas 已显示时返回 canvas，否则截图 img。
        const canvas = el('aoa-screen-canvas');
        if (state.active && canvas && !canvas.hidden && canvas.dataset.aoaFrameReady) return canvas;
        return null;
    }

    // __state 仅供运行时 UI 测试注入/读取会话内部状态（framesDecoded、
    // lastPongAt），生产代码不得依赖。
    window.aoaLiveScreen = {start, stop, activeSurface, isActive: () => state.active, __state: state};
})();
