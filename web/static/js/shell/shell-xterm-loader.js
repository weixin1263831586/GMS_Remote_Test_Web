    // 动态加载 xterm.js（仅在访问终端页面时）
    function loadXTermScripts() {
        if (window.xtermLoadPromise) return window.xtermLoadPromise;
        if (window.xtermLoaded && typeof Terminal !== 'undefined' && typeof FitAddon !== 'undefined') {
            return Promise.resolve();
        }

        window.xtermLoadPromise = new Promise((resolve, reject) => {
            const script1 = document.createElement('script');
            script1.src = '/static/vendor/xterm/xterm.min.js';
            script1.onload = function() {
                const script2 = document.createElement('script');
                script2.src = '/static/vendor/xterm/xterm-addon-fit.min.js';
                script2.onload = function() {
                    window.xtermLoaded = true;
                    resolve();
                };
                script2.onerror = function(error) {
                    window.xtermLoadPromise = null;
                    reject(error);
                };
                document.head.appendChild(script2);
            };
            script1.onerror = function(error) {
                window.xtermLoadPromise = null;
                reject(error);
            };
            document.head.appendChild(script1);
        });

        return window.xtermLoadPromise;
    }

    // 标记：xterm.js 尚未加载
    window.xtermLoaded = false;
    