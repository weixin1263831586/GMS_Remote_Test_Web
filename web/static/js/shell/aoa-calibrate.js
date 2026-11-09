// 命令坐标标定（②列「定位」按钮）：可视化定位，不执行、不碰设备。
//
// AOA HID 描述符是触摸屏（无悬停/相对移动报文），设备端做不到"只移动
// 不点击"的可见反馈；标定改为投屏叠加层上的准星：红色圆环标记当前命令
// 将点击的逻辑坐标，用户点击投屏画面重新取点（录制路径在 aoa-recorder.js
// 拦截，不走 /record/input），确认后把坐标按轴换算回写命令文本。
// 拆自 shell-aoa.js（前端体积 ratchet）：只依赖 shell-aoa.js 的共享状态
// （aoaPanelState/aoaEl/aoaSetStatus/aoaHidMetadata/aoaDeviceContext/
// aoaRecorderTarget/aoaRenderCommands/aoaCommandEditFinish），函数在
// 运行时调用，加载顺序须在 shell-aoa.js 之前（与 aoa-recorder.js 同模式）。
'use strict';

// ---- 命令坐标标定（可视化定位，不执行、不碰设备）----
//
// AOA HID 描述符是触摸屏（无悬停/相对移动报文），设备端做不到"只移动
// 不点击"的可见反馈；标定改为投屏叠加层上的准星：红色圆环标记当前命令
// 将点击的逻辑坐标，用户点击投屏画面重新取点（不走录制执行路径），确认
// 后把坐标按轴换算回写命令文本。保存时机与命令编辑一致（编辑器收口时）。

const AOA_CALIBRATE_MAX_X = 360;
const AOA_CALIBRATE_MAX_Y = 640;

function aoaCommandCalibrateStart(index) {
    if (aoaPanelState.running || aoaPanelState.playbackActive || !aoaCommandEditFinish()) return;
    const command = aoaPanelState.commands[index];
    if (typeof command !== 'string') return;
    const match = command.trim().match(/^(?:click|longClick)\s+(\d{1,3})\s+(\d{1,3})$/);
    if (!match) {
        aoaSetStatus(`第 ${index + 1} 条不是点击命令（click/longClick X Y），无法标定坐标`, 'warning');
        return;
    }
    const list = aoaEl('aoa-command-list'); const scroll = list.scrollTop;
    aoaPanelState.commandCalibrate = {
        index, original: command,
        x: Number(match[1]), y: Number(match[2]),
    };
    if (typeof window.aoaScreenGridCalibrate === 'function') {
        window.aoaScreenGridCalibrate(aoaPanelState.commandCalibrate);
    }
    aoaRenderCommands(); list.scrollTop = scroll;
    aoaSetStatus(`标定第 ${index + 1} 条：投屏上已显示坐标准星，单击画面取点，确认后点「保存」`);
}

// 投屏画面点击取点（录制路径拦截时调用）：更新准星与待保存坐标。
function aoaCommandCalibratePick(point) {
    const calibrate = aoaPanelState.commandCalibrate;
    if (!calibrate) return false;
    calibrate.x = Math.max(0, Math.min(AOA_CALIBRATE_MAX_X, point.x));
    calibrate.y = Math.max(0, Math.min(AOA_CALIBRATE_MAX_Y, point.y));
    if (typeof window.aoaScreenGridCalibrate === 'function') {
        window.aoaScreenGridCalibrate(calibrate);
    }
    aoaSetStatus(`标定第 ${calibrate.index + 1} 条：新坐标 ${calibrate.x},${calibrate.y}（点「保存」生效，或继续取点）`);
    return true;
}

// 标定模式已失效（命令被改/删、列表被载入重置）：自动清准星。
function aoaCommandCalibrateStale() {
    const calibrate = aoaPanelState.commandCalibrate;
    if (!calibrate) return false;
    if (aoaPanelState.commands[calibrate.index] !== calibrate.original) {
        aoaCommandCalibrateCancel();
        return true;
    }
    return false;
}

// 标定取点的显示分辨率：标定 context 优先，其次设备清单，缺省退回视频帧
// 像素（与网格读数同样的估算口径，仅影响换算精度，不影响取点本身）。
function aoaCalibrateDisplaySize() {
    const context = aoaPanelState.calibration?.context;
    if (Array.isArray(context?.display) && context.display.length === 2
            && context.display.every(value => Number.isInteger(value) && value > 0)) {
        return {width: context.display[0], height: context.display[1]};
    }
    try {
        const target = aoaRecorderTarget();
        const deviceContext = aoaDeviceContext(target.device, target.workerId);
        if (deviceContext?.display?.length === 2
                && deviceContext.display.every(value => Number.isInteger(value) && value > 0)) {
            return {width: deviceContext.display[0], height: deviceContext.display[1]};
        }
    } catch (error) { /* 未选录制设备：走 frame 兜底 */ }
    return null;
}

// 视频帧像素 → AOA HID 逻辑坐标（官方按轴映射的逆变换：direct 361、
// pointer 360，与 foundation.aoa_profile.real_to_aoa_point 一致）。
function aoaCalibrateLogicalPoint(frame, point) {
    const display = aoaCalibrateDisplaySize() || {width: frame.width, height: frame.height};
    const direct = (aoaHidMetadata().hid_touch_mode || 'direct') === 'direct';
    return {
        x: Math.max(0, Math.min(AOA_CALIBRATE_MAX_X,
            Math.round(point.x * (AOA_CALIBRATE_MAX_X + (direct ? 1 : 0)) / display.width))),
        y: Math.max(0, Math.min(AOA_CALIBRATE_MAX_Y,
            Math.round(point.y * (AOA_CALIBRATE_MAX_Y + (direct ? 1 : 0)) / display.height))),
    };
}

function aoaCommandCalibrateCancel() {
    if (!aoaPanelState.commandCalibrate) return;
    aoaPanelState.commandCalibrate = null;
    if (typeof window.aoaScreenGridCalibrate === 'function') {
        window.aoaScreenGridCalibrate(null);
    }
}

function aoaCommandCalibrateFinish(save = true) {
    const calibrate = aoaPanelState.commandCalibrate;
    if (!calibrate) return true;
    if (aoaPanelState.running || aoaPanelState.playbackActive) return false;
    if (save && aoaPanelState.commands[calibrate.index] === calibrate.original) {
        const x = Math.max(0, Math.min(AOA_CALIBRATE_MAX_X, Math.round(calibrate.x)));
        const y = Math.max(0, Math.min(AOA_CALIBRATE_MAX_Y, Math.round(calibrate.y)));
        // 保留命令原类型（click/longClick）：标定只调整坐标，不能把长按
        // 命令回写成普通点击，否则依赖长按的回放（开发者选项等）会静默变义。
        const kind = (String(calibrate.original || '').trim()
            .match(/^(click|longClick)\s+\d+\s+\d+$/) || [])[1] || 'click';
        aoaPanelState.commands[calibrate.index] = `${kind} ${x} ${y}`;
        aoaPanelState.editingProfileId = '';
        aoaSetStatus(`第 ${calibrate.index + 1} 条已标定为 ${kind} ${x} ${y}，保存配置后可持久保留`);
    }
    aoaCommandCalibrateCancel();
    aoaRenderCommands();
    return true;
}
