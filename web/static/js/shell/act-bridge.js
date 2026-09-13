/**
 * act-bridge —— CSP 安全的事件委托桥。
 *
 * 用途：把历史上的 inline 事件处理器（形如 data-click 出现之前的
 * onclick 字符串属性）迁移为
 * 声明式属性，由本模块在 document 上统一委托分发。inline handler
 * 与 eval 同属 CSP `unsafe-inline`/`unsafe-eval` 管制面；迁移完成后
 * 生产 CSP 的 script-src 即可去掉 'unsafe-inline'。
 *
 * 属性约定（元素上声明，委托分发）：
 *   data-click / data-change / data-input / data-submit /
 *   data-keydown / data-keyup / data-keypress / data-dblclick /
 *   data-toggle / data-focus / data-blur
 *       值 = 要调用的全局函数名（必须已在 window 上，含惰性解析——
 *       分发时才查找，允许函数晚于标记定义）。
 *   data-a0..data-an  按位实参，自动类型推断：
 *       "true"/"false"/"null" → 布尔/null；纯数字 → number；其余原样字符串。
 *   data-r0..data-rn  元素相对实参（优先于同位 data-aN）：
 *       "value" | "checked" | "el"（元素自身，等价旧 handler 里的 this）
 *       | "event"（事件对象）。
 *   data-key          keydown/keyup/keypress 过滤器：keyCode 数字（如 "13"）
 *                     或 key 名（如 "Enter"）；不匹配时静默跳过。
 *   data-prevent      存在即先 event.preventDefault()（等价旧 handler
 *                     开头的 event.preventDefault(); 语句）。
 *   data-stop         存在即先 event.stopPropagation()（等价旧 handler
 *                     开头的 event.stopPropagation(); 语句）。
 *   data-hover-*      mouseover/mouseout 样式切换见 _bindHover。
 *
 * 分发语义与 inline handler 对齐：函数内 `this` = 触发元素。
 */
(function () {
    'use strict';

    var DELEGATED = ['click', 'change', 'input', 'submit', 'keydown', 'keyup',
        'keypress', 'dblclick', 'dragstart', 'dragend', 'dragover',
        'dragenter', 'drop'];
    // focus/blur/error/toggle 不冒泡（toggle 在 details 上触发）；
    // 资源 error/load 仅在捕获阶段经过祖先。
    var CAPTURED = ['focus', 'blur', 'error', 'load', 'mouseover', 'mouseout',
        'toggle'];

    function resolveFn(name) {
        try {
            var fn = window[name];
            return typeof fn === 'function' ? fn : null;
        } catch (e) {
            return null;
        }
    }

    function literalArg(raw) {
        if (raw === 'true') return true;
        if (raw === 'false') return false;
        if (raw === 'null') return null;
        if (/^-?\d+(?:\.\d+)?$/.test(raw)) return Number(raw);
        return raw;
    }

    function marshalArgs(el, event) {
        var args = [];
        for (var i = 0; ; i++) {
            var ref = el.getAttribute('data-r' + i);
            if (ref !== null) {
                if (ref === 'el') args.push(el);
                else if (ref === 'event') args.push(event);
                else if (ref === 'value') args.push(el.value);
                else if (ref === 'checked') args.push(el.checked);
                else if (ref.indexOf('.') !== -1) {
                    // 点路径（如 dataset.serial）：逐级属性访问。
                    var cur = el;
                    var parts = ref.split('.');
                    for (var p = 0; p < parts.length; p++) {
                        cur = cur ? cur[parts[p]] : undefined;
                    }
                    args.push(cur);
                } else args.push(el[ref]);
                continue;
            }
            var raw = el.getAttribute('data-a' + i);
            if (raw === null) break;
            args.push(literalArg(raw));
        }
        return args;
    }

    function keyMatches(event, filter) {
        var code = event.keyCode || event.which;
        var key = event.key;
        var parts = filter.split(',');
        for (var i = 0; i < parts.length; i++) {
            var part = parts[i].trim();
            if (!part) continue;
            if (/^\d+$/.test(part)) {
                if (code === Number(part)) return true;
            } else if (key === part) {
                return true;
            }
        }
        return false;
    }

    function dispatch(el, event, type) {
        var name = el.getAttribute('data-' + type);
        if (!name) return;
        if ((type === 'keydown' || type === 'keyup' || type === 'keypress')) {
            var filter = el.getAttribute('data-key');
            if (filter !== null && !keyMatches(event, filter)) return;
        }
        var fn = resolveFn(name);
        if (!fn) {
            console.warn('[act-bridge] no global function for data-' + type + '="' + name + '"');
            return;
        }
        if (el.hasAttribute('data-prevent')) event.preventDefault();
        if (el.hasAttribute('data-stop')) event.stopPropagation();
        fn.apply(el, marshalArgs(el, event));
    }

    function handle(event, type) {
        var target = event.target;
        if (!(target && target.closest)) return;
        // submit 目标是 form 自身；closest 覆盖内嵌按钮触发冒泡的场景。
        var el = target.closest('[data-' + type + ']');
        if (el) dispatch(el, event, type);
    }

    DELEGATED.forEach(function (type) {
        document.addEventListener(type, function (event) {
            handle(event, type);
        });
    });
    CAPTURED.forEach(function (type) {
        document.addEventListener(type, function (event) {
            handle(event, type);
        }, true);
    });
})();
