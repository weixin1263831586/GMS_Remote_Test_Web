/**
 * aoa-screen-grid —— 投屏画面上的 AOA 命令坐标网格（纯展示叠加层）。
 *
 * AOA pointer/direct 命令的坐标空间是 0..360 × 0..640
 * （AOA_TOUCH_MAX_*，foundation/aoa_profile.py 单一真值；direct 模式的
 * 361/641 上界由 Worker 侧换算处理，网格仍按 pointer 空间标注）。
 * 网格把该坐标空间直接画在投屏内容框上：左上角 (0,0)，右下角 (360,640)，
 * 悬停显示光标处的命令坐标读数，便于把录制点位与 AOA 命令对上。
 *
 * 约束：本模块不注入任何设备事件、不参与录制输入路径（overlay
 * pointer-events:none，input 换算仍在 aoa-recorder.js 的 frame 空间）。
 * 「标定标记」是命令坐标的可视化引用（aoa-calibrate.js 标定模式经
 * aoaScreenGridCalibrate 下发坐标）：红色准星画在命令坐标处，点击画面
 * 改写命令坐标的交互由录制路径拦截，本模块只画不拦。
 * 几何按 object-fit: contain 的实际内容框计算（黑边不计入网格），
 * 布局尺寸变化经 ResizeObserver 自动重画；开关状态本地持久化。
 */
(function () {
    'use strict';

    const GRID_MAX_X = 360;
    const GRID_MAX_Y = 640;
    const PREF_KEY = 'aoa-screen-grid';

    const state = {
        enabled: null,      // null = 尚未从本地偏好初始化
        hover: null,
        calibrate: null,    // {index, x, y}：命令坐标标定标记（AOA 逻辑坐标）
        observer: null,
        initTries: 0,
    };

    function el(id) { return document.getElementById(id); }

// 网格步长：让两条刻度线之间的像素距离尽量均匀可读（≤9 格）。
    function maxStep(axisMax) {
        for (const step of [10, 20, 30, 40, 60, 80, 120, 180]) {
            if (axisMax / step <= 9) return step;
        }
        return 240;
    }

    function activeMedia() {
        const canvas = el('aoa-screen-canvas');
        if (canvas && !canvas.hidden && canvas.dataset.aoaFrameReady && canvas.width) return canvas;
        const img = el('aoa-screen-image');
        if (img && !img.hidden && img.naturalWidth) return img;
        return null;
    }

    function contentBox(stage, media) {
        // object-fit: contain 的内容框（相对 stage，去除元素盒内的黑边）。
        const rect = media.getBoundingClientRect();
        const stageRect = stage.getBoundingClientRect();
        const frameW = media.width || media.naturalWidth;
        const frameH = media.height || media.naturalHeight;
        if (!rect.width || !rect.height || !frameW || !frameH) return null;
        const scale = Math.min(rect.width / frameW, rect.height / frameH);
        const w = frameW * scale;
        const h = frameH * scale;
        return {left: rect.left - stageRect.left + (rect.width - w) / 2,
            top: rect.top - stageRect.top + (rect.height - h) / 2, w, h};
    }

    function label(ctx, text, x, y) {
        const width = ctx.measureText(text).width;
        ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
        ctx.fillRect(x - 1, y - 1, width + 3, 12);
        ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
        ctx.fillText(text, x, y);
    }

    function draw() {
        const overlay = el('aoa-screen-grid');
        const stage = el('aoa-screen-stage');
        if (!overlay || !stage) return;
        const showGrid = state.enabled;
        const showMark = Boolean(state.calibrate);
        const media = showGrid || showMark ? activeMedia() : null;
        if (!media) { overlay.hidden = true; state.hover = null; return; }
        const box = contentBox(stage, media);
        if (!box) { overlay.hidden = true; return; }
        // 覆盖整个 stage（含黑边）：网格线仍只落在画面内容框内，
        // 刻度数字画在画面外侧的黑边上，不遮挡设备画面。
        const dpr = window.devicePixelRatio || 1;
        const width = Math.max(box.w, stage.clientWidth);
        const height = Math.max(box.h, stage.clientHeight);
        overlay.width = Math.max(1, Math.round(width * dpr));
        overlay.height = Math.max(1, Math.round(height * dpr));
        overlay.style.left = '0px';
        overlay.style.top = '0px';
        overlay.style.width = `${width}px`;
        overlay.style.height = `${height}px`;
        overlay.hidden = false;

        const ctx = overlay.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
        ctx.textBaseline = 'top';
        ctx.lineWidth = 1;
        ctx.textAlign = 'center';
        if (showGrid) {
            drawGridLines(ctx, box, width, height);
        }
        if (showMark) drawCalibrateMark(ctx, box);
        if (!showGrid && !showMark) overlay.hidden = true;
    }

    function drawGridLines(ctx, box, width, height) {
        const MARGIN = 14;
        const stepX = maxStep(GRID_MAX_X);
        const stepY = maxStep(GRID_MAX_Y);
        // 黑边余量：某侧有足够黑边时刻度画在外面，贴边时回退到画面内
        // 半透明角标（bottom 标注/hover 读数同理），绝不被 stage 裁掉。
        const topBar = Math.max(0, box.top);
        const leftBar = Math.max(0, box.left);
        const bottomBar = Math.max(0, height - (box.top + box.h));
        for (let x = stepX; x < GRID_MAX_X; x += stepX) {
            const px = box.left + (x / GRID_MAX_X) * box.w;
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.28)';
            ctx.beginPath();
            ctx.moveTo(px + 0.5, box.top); ctx.lineTo(px + 0.5, box.top + box.h);
            ctx.stroke();
            if (topBar >= MARGIN - 2) {
                label(ctx, String(x), px, box.top - 12);
            } else {
                // 顶部贴边：刻度画在画面内最上沿，配底衬保证可读。
                label(ctx, String(x), px, 2);
            }
        }
        for (let y = stepY; y < GRID_MAX_Y; y += stepY) {
            const py = box.top + (y / GRID_MAX_Y) * box.h;
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.28)';
            ctx.beginPath();
            ctx.moveTo(box.left, py + 0.5); ctx.lineTo(box.left + box.w, py + 0.5);
            ctx.stroke();
            if (leftBar >= 46) {
                ctx.textAlign = 'right';
                label(ctx, String(y), box.left - 6, py - 4);
                ctx.textAlign = 'center';
            } else {
                label(ctx, String(y), box.left + 2, py - 4);
            }
        }
        if (leftBar >= 46) {
            ctx.textAlign = 'right';
            label(ctx, '0,0', box.left - 6, topBar >= MARGIN - 2 ? box.top - 12 : box.top + 2);
            ctx.textAlign = 'center';
        } else {
            label(ctx, '0,0', box.left + 2, topBar >= MARGIN - 2 ? box.top - 12 : box.top + 2);
        }
        // 终点标注：有底部黑边画在下方，贴边时收进画面右下角。
        const maxX = box.left + box.w;
        const maxY = box.top + box.h;
        label(ctx, `${GRID_MAX_X},${GRID_MAX_Y}`, maxX - 28,
            bottomBar >= 16 ? maxY + 2 : maxY - 14);

        if (state.hover) drawHover(ctx, box, bottomBar);
    }

    function drawHover(ctx, box, bottomBar) {
        const {x, y} = state.hover;
        const px = box.left + (x / GRID_MAX_X) * box.w;
        const py = box.top + (y / GRID_MAX_Y) * box.h;
        ctx.strokeStyle = 'rgba(56, 189, 248, 0.9)';
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(px + 0.5, box.top); ctx.lineTo(px + 0.5, box.top + box.h);
        ctx.moveTo(box.left, py + 0.5); ctx.lineTo(box.left + box.w, py + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
        // 读数在底部黑边；贴边时收进画面左下角（带底衬，不被裁剪）。
        const text = `x=${x}  y=${y}`;
        const width = ctx.measureText(text).width;
        const inside = bottomBar < 16;
        const bx = Math.min(box.left + 2, box.left + box.w - width - 12);
        const by = inside ? box.top + box.h - 16 : box.top + box.h + 2;
        ctx.fillStyle = 'rgba(8, 47, 73, 0.85)';
        ctx.fillRect(bx, by, width + 10, 14);
        ctx.fillStyle = '#7dd3fc';
        ctx.fillText(text, bx + 5, by + 3);
    }

    function aoaCoord(event) {
        const media = activeMedia();
        const stage = el('aoa-screen-stage');
        if (!media || !stage) return null;
        const box = contentBox(stage, media);
        if (!box) return null;
        const stageRect = stage.getBoundingClientRect();
        const x = (event.clientX - stageRect.left - box.left) / box.w * GRID_MAX_X;
        const y = (event.clientY - stageRect.top - box.top) / box.h * GRID_MAX_Y;
        if (x < 0 || y < 0 || x > GRID_MAX_X || y > GRID_MAX_Y) return null;
        return {x: Math.round(x), y: Math.round(y)};
    }

    function drawCalibrateMark(ctx, box) {
        const mark = state.calibrate;
        if (!mark) return;
        const px = box.left + (mark.x / GRID_MAX_X) * box.w;
        const py = box.top + (mark.y / GRID_MAX_Y) * box.h;
        // 红色准星：当前选中命令将点击的 AOA 逻辑坐标。与白色网格线/蓝色
        // 悬停十字高对比；圆环直径随画面缩放，小画面下仍可辨识。
        const ring = Math.max(9, Math.min(box.w, box.h) * 0.028);
        ctx.save();
        ctx.strokeStyle = 'rgba(239, 68, 68, 0.95)';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(px, py, ring, 0, Math.PI * 2);
        ctx.stroke();
        ctx.strokeStyle = 'rgba(239, 68, 68, 0.85)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 2]);
        ctx.beginPath();
        ctx.moveTo(px + 0.5, box.top); ctx.lineTo(px + 0.5, box.top + box.h);
        ctx.moveTo(box.left, py + 0.5); ctx.lineTo(box.left + box.w, py + 0.5);
        ctx.stroke();
        ctx.restore();
        // 读数贴准星右上；贴近右/下边缘时收进画面内，保证不被裁剪。
        const text = `#${mark.index + 1}  ${mark.x},${mark.y}`;
        const width = ctx.measureText(text).width + 8;
        const tx = px + ring + 3 + width > box.left + box.w
            ? px - ring - 3 - width : px + ring + 3;
        const ty = Math.min(Math.max(box.top + 2, py - 7), box.top + box.h - 16);
        ctx.fillStyle = 'rgba(127, 29, 29, 0.9)';
        ctx.fillRect(tx, ty, width, 14);
        ctx.fillStyle = '#fecaca';
        ctx.fillText(text, tx + 4, ty + 3);
    }

    // 标定标记入口（aoa-calibrate.js 标定模式调用）：传入 null 清除。
    function aoaScreenGridCalibrate(mark) {
        state.calibrate = mark && Number.isFinite(mark.x) && Number.isFinite(mark.y)
            ? {index: mark.index, x: Math.round(mark.x), y: Math.round(mark.y)}
            : null;
        draw();
    }

    function syncButton() {
        const button = el('aoa-grid-toggle');
        if (button) button.textContent = state.enabled ? '⊞ 网格开' : '⊞ 网格关';
    }

    function aoaToggleScreenGrid() {
        state.enabled = !state.enabled;
        try { localStorage.setItem(PREF_KEY, state.enabled ? '1' : '0'); } catch (error) { /* 隐私模式忽略 */ }
        syncButton();
        draw();
    }

    function init() {
        if (state.observer || !el('aoa-screen-stage')) {
            if (!el('aoa-screen-stage') && state.initTries < 40) {
                state.initTries += 1;
                setTimeout(init, 250);
            }
            return;
        }
        try { state.enabled = localStorage.getItem(PREF_KEY) !== '0'; } catch (error) { state.enabled = true; }
        syncButton();
        state.observer = new ResizeObserver(() => draw());
        state.observer.observe(el('aoa-screen-stage'));
        const canvas = el('aoa-screen-canvas');
        const image = el('aoa-screen-image');
        if (canvas) state.observer.observe(canvas);
        if (image) state.observer.observe(image);
        window.addEventListener('resize', draw);
        const stage = el('aoa-screen-stage');
        stage.addEventListener('pointermove', event => {
            if (!state.enabled) return;
            state.hover = aoaCoord(event);
            draw();
        });
        stage.addEventListener('pointerleave', () => {
            if (!state.enabled && !state.calibrate) return;
            state.hover = null;
            draw();
        });
        draw();
    }

    window.aoaToggleScreenGrid = aoaToggleScreenGrid;
    window.aoaScreenGridCalibrate = aoaScreenGridCalibrate;
    // 运行时测试与调试检查入口（只读状态，不触发重画）。
    window.aoaScreenGridState = () => ({
        enabled: state.enabled, hover: state.hover, calibrate: state.calibrate,
        media: Boolean(activeMedia()),
        overlayHidden: el('aoa-screen-grid')?.hidden ?? null,
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
