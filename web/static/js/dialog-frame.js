// Decorative geometry uses actual CSS pixels so cuts and accents always meet.
(function () {
    'use strict';
    const namespace = 'http://www.w3.org/2000/svg';
    const sizes = new WeakMap();
    function path(svg, className) {
        let element = svg.querySelector('path.' + className);
        if (!element) {
            element = document.createElementNS(namespace, 'path');
            element.setAttribute('class', className);
            element.setAttribute('vector-effect', 'non-scaling-stroke');
            svg.append(element);
        }
        return element;
    }
    function draw(svg) {
        const {width, height} = svg.getBoundingClientRect();
        if (!svg.isConnected || width < 32 || height < 12) return;
        const key = `${width}:${height}`;
        if (sizes.get(svg) === key) return;
        sizes.set(svg, key);
        svg.setAttribute('data-frame-ready', 'true');
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        if (svg.classList.contains('tech-frame')) {
            const edge = svg.querySelector('path');
            edge.setAttribute('d', `M16 2H${width - 16}L${width - 2} 16V${height - 16}L${width - 16} ${height - 2}H16L2 ${height - 16}V16Z`);
            path(svg, 'tech-frame-accent').setAttribute('d',
                `M2 28V16L16 2H28M${width - 28} ${height - 2}H${width - 16}L${width - 2} ${height - 16}V${height - 28}`);

        } else {
            const step = Math.max(60, Math.round(width * .49));
            const end = Math.max(step + 20, width - 104);
            const baseline = 15;
            svg.querySelector('path').setAttribute('d', `M0 ${baseline}H${step}L${step + 14} 1H${end}`);
            // The active underline is exactly coincident with the first segment
            // of the path, not a separate CSS border with a different Y origin.
            path(svg, 'tech-rule-highlight').setAttribute('d',
                `M18 ${baseline}H${Math.min(102, step)}`);
            path(svg, 'tech-rule-ticks').setAttribute('d',
                [0, 8, 16, 24].map(offset => `M${end + 10 + offset} 10l7 -7`).join(''));
        }
    }
    const resize = new ResizeObserver(entries => entries.forEach(entry => draw(entry.target)));
    function frames(root) {
        if (!(root instanceof Element || root instanceof Document)) return [];
        const found = [...root.querySelectorAll('.tech-frame,.tech-header-rule')];
        if (root.matches?.('.tech-frame,.tech-header-rule')) found.unshift(root);
        return found;
    }
    const changes = new MutationObserver(records => {
        records.forEach(record => {
            record.removedNodes.forEach(node => frames(node).forEach(svg => resize.unobserve(svg)));
            record.addedNodes.forEach(node => frames(node).forEach(svg => { resize.observe(svg); draw(svg); }));
        });
    });
    frames(document).forEach(svg => { resize.observe(svg); draw(svg); });
    changes.observe(document.documentElement, {childList: true, subtree: true});
})();
