// Shell 模块：自动获取网站图标（从 shell.html 内联脚本尾部提取）。
// ==================== 自动获取网站图标 ====================
const commonSiteEmojis = {
    // 使用真实 SVG 图标的网站
    'github.com': 'https://github.githubassets.com/favicons/favicon.svg',
    'gitlab.com': 'https://gitlab.com/assets/favicon-72a2cad5025aa931d6ea56c3201d1f18e68a8cd0feb995d52ae46c2e0a647592.png',
    'google.com': 'https://www.google.com/favicon.ico',
    'youtube.com': 'https://www.youtube.com/favicon.ico',
    'twitter.com': 'https://twitter.com/favicon.ico',
    'facebook.com': 'https://static.facebook.com/images/notifications/favicon.ico',
    'linkedin.com': 'https://static.licdn.com/sc/h/al2o9zrvru7aqj8e1x2rzsrca',
    'stackoverflow.com': 'https://stackoverflow.com/favicon.ico',
    'reddit.com': 'https://www.redditstatic.com/desktop2x/img/favicon/favicon-32x32.png',
    'wikipedia.org': 'https://www.wikipedia.org/favicon.ico',
    'amazon.com': 'https://www.amazon.com/favicon.ico',
    'netflix.com': 'https://assets.nflxext.com/us/ffe/siteui/common/icons/nficon2023.png',
    'spotify.com': 'https://open.spotifycdn.com/cdn/images/favicon32.35b96dcb4ff7c2caa75b5cf448b60e3e.png',
    'discord.com': 'https://discord.com/assets/8c9701d8b9fe777475d6d69cc5e0c91f.ico',
    'slack.com': 'https://slack.com/favicon.ico',
    'microsoft.com': 'https://www.microsoft.com/favicon.ico',
    'apple.com': 'https://www.apple.com/favicon.ico',
    'npmjs.com': 'https://static.npmjs.com/faviticon/favicon-32x32.png',
    'docker.com': 'https://www.docker.com/favicon.ico',
    'kubernetes.io': 'https://kubernetes.io/favicon.ico',
    'redis.io': 'https://redis.io/favicon.ico',
    'mongodb.com': 'https://www.mongodb.com/favicon.ico',
    'mysql.com': 'https://www.mysql.com/favicon.ico',
    'postgresql.org': 'https://www.postgresql.org/favicon.ico',
    'nginx.org': 'https://nginx.org/favicon.ico',
    'apache.org': 'https://www.apache.org/favicon.ico',
    'python.org': 'https://www.python.org/favicon.ico',
    'java.com': 'https://www.java.com/favicon.ico',
    'golang.org': 'https://go.dev/favicon.ico',
    'rust-lang.org': 'https://www.rust-lang.org/favicon.ico',
    'grafana.com': 'https://grafana.com/favicon.ico',
    'prometheus.io': 'https://prometheus.io/favicon.ico',
    'jenkins.io': 'https://www.jenkins.io/favicon.ico',
    'android.com': 'https://developer.android.com/favicon.ico',
    'aws.amazon.com': 'https://aws.amazon.com/favicon.ico',
    'azure.microsoft.com': 'https://azure.microsoft.com/favicon.ico',
    'digitalocean.com': 'https://www.digitalocean.com/favicon.ico',

    // 备用 Emoji
    'bitbucket.org': '🪣',
    'vercel.com': '▲',
    'netlify.com': '▲',
    'heroku.com': '🟣',
    'shopify.com': '🛒',
    'wordpress.org': '📝',
    'woocommerce.com': '🛒',
    'visualstudio.com': '🎨',
    'firebase.google.com': '🔥',
    'linux.org': '🐧',
    'javascript.com': '💛',
    'rust-lang.org': '🦀',
    'golang.org': '🐹'
};

const iconHtmlEscapeMap = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
};

function escapeIconAttr(value) {
    return String(value || '').replace(/[&<>"']/g, char => iconHtmlEscapeMap[char]);
}

function isRemoteIconUrl(iconValue) {
    return typeof iconValue === 'string' && /^https?:\/\//i.test(iconValue);
}

function isImageIconUrl(iconValue) {
    return typeof iconValue === 'string' && (
        iconValue.startsWith('/static/') ||
        iconValue.startsWith('/api/favicon/proxy') ||
        isRemoteIconUrl(iconValue)
    );
}

function proxiedIconUrl(iconValue) {
    if (isRemoteIconUrl(iconValue)) {
        return `/api/favicon/proxy?timeout=2&url=${encodeURIComponent(iconValue)}`;
    }
    return iconValue;
}

function unwrapIconValue(iconValue) {
    if (typeof iconValue !== 'string') return '';
    if (iconValue.startsWith('[img:')) {
        return iconValue.replace('[img:', '').replace(']', '');
    }
    if (iconValue.startsWith('[favicon:')) {
        return iconValue.replace('[favicon:', '').replace(']', '');
    }
    return iconValue;
}

function renderIconImage(iconValue, altText = '图标', size = '100%') {
    const src = escapeIconAttr(proxiedIconUrl(unwrapIconValue(iconValue)));
    const alt = escapeIconAttr(altText);
    return `<img src="${src}" alt="${alt}" style="width: ${size}; height: ${size}; object-fit: contain;" onerror="this.parentElement.textContent='🌐'">`;
}

const LEGACY_TOOL_ICON_OVERRIDES = {
    'Rockchip OA': '/static/icons/favicons/3a57911f43fd8a9ccfaa8e3ed6cf6cb3461308d7a37e13775ab6414b785725c9.ico',
    'Redmine': '/static/icons/favicons/1a43fcd157ab39c1746959e9ea5f993614b296573069cd78b5163c22dcbfa00c.png',
    'Gerrit': '/static/icons/favicons/rockchip-gerrit.svg',
    'OpenGrok': '/static/icons/favicons/rockchip-opengrok.svg',
    'Remote Run Server': '/static/icons/favicons/rockchip-remote-run.svg',
    'CRM': '/static/icons/favicons/rockchip-crm.svg',
    '路由器端口转发': '/static/icons/favicons/rockchip-router.svg'
};

const LEGACY_BROWSER_ICON_CANDIDATES = {
    'Gerrit': '/static/icons/favicons/rockchip-gerrit.svg',
    'OpenGrok': '/static/icons/favicons/rockchip-opengrok.svg',
    'CRM': EXTERNAL_SERVICES.crm_icon_url || '',
    '路由器端口转发': EXTERNAL_SERVICES.router_icon_url || ''
};
const hasConfiguredBranding = Object.prototype.hasOwnProperty.call(PRODUCT_BRANDING, 'company_name');
const COMPANY_CATEGORY = String(
    PRODUCT_BRANDING.company_name || (hasConfiguredBranding ? 'Organization' : 'Rockchip'),
);
const COMPANY_HOME_URL = String(
    PRODUCT_BRANDING.company_url || (hasConfiguredBranding ? '' : 'https://www.rock-chips.com'),
);
const COMPANY_ICON = String(PRODUCT_BRANDING.company_icon || '🏢');
const COMPANY_KEYWORDS = Array.isArray(PRODUCT_BRANDING.company_keywords)
    ? PRODUCT_BRANDING.company_keywords.map(value => String(value).toLowerCase()).filter(Boolean)
    : (hasConfiguredBranding ? [] : ['rockchip', 'rock-chips.com']);
const PRODUCT_TOOL_ICON_OVERRIDES = PRODUCT_BRANDING.tool_icon_overrides
    && typeof PRODUCT_BRANDING.tool_icon_overrides === 'object'
    ? PRODUCT_BRANDING.tool_icon_overrides
    : (hasConfiguredBranding ? {} : LEGACY_TOOL_ICON_OVERRIDES);
const PRODUCT_BROWSER_ICON_CANDIDATES = PRODUCT_BRANDING.browser_icon_candidates
    && typeof PRODUCT_BRANDING.browser_icon_candidates === 'object'
    ? PRODUCT_BRANDING.browser_icon_candidates
    : (hasConfiguredBranding ? {} : LEGACY_BROWSER_ICON_CANDIDATES);

function renderIconImageWithFallback(primaryIcon, fallbackIcon, altText = '图标', size = '100%') {
    const primarySrc = escapeIconAttr(unwrapIconValue(primaryIcon));
    const fallbackSrc = escapeIconAttr(proxiedIconUrl(unwrapIconValue(fallbackIcon)));
    const alt = escapeIconAttr(altText);
    return `<img src="${primarySrc}" data-fallback-src="${fallbackSrc}" alt="${alt}" style="width: ${size}; height: ${size}; object-fit: contain;" onerror="const fb=this.getAttribute('data-fallback-src'); if(fb && this.src.indexOf(fb) === -1){this.src=fb;}else{this.parentElement.textContent='🌐';}">`;
}

function getDisplayToolIcon(icon, tool) {
    const overrideIcon = tool?.title ? PRODUCT_TOOL_ICON_OVERRIDES[tool.title] : '';
    const rawIcon = icon || '';
    const unwrappedIcon = unwrapIconValue(rawIcon);
    if (overrideIcon && (
        !rawIcon ||
        rawIcon === '🌐' ||
        rawIcon === '/static/icons/site-default.svg' ||
        rawIcon.startsWith('/api/favicon/proxy') ||
        isRemoteIconUrl(unwrappedIcon)
    )) {
        return overrideIcon;
    }
    return rawIcon;
}

async function autoFetchIcon(url) {
    const urlInput = document.getElementById('tool-url');
    const iconInput = document.getElementById('tool-icon');
    const preview = document.getElementById('icon-preview');
    const previewImage = document.getElementById('preview-image');

    if (!url || !url.trim()) return;

    try {
        const urlObj = new URL(url);
        const domain = urlObj.hostname;

        try {
            const response = await fetch(`/api/favicon/fetch?url=${encodeURIComponent(url)}`);
            if (response.ok) {
                const result = await response.json();
                const iconData = result.data || {};
                if (result.success && iconData.icon_url) {
                    iconInput.value = iconData.icon_url;
                    showIconPreview(iconInput.value);
                    debugLog(`✅ 自动获取图标成功: ${iconData.icon_url} (${iconData.icon_type}, ${iconData.source})`);
                    return;
                }
            }
        } catch (apiError) {
            console.warn('API获取图标失败，使用本地兜底:', apiError);
        }

        iconInput.value = commonSiteEmojis[domain] || '/static/icons/site-default.svg';
        showIconPreview(iconInput.value);

    } catch (e) {
        console.warn('URL 解析失败:', e);
    }
}




function showIconPreview(iconValue) {
    const preview = document.getElementById('icon-preview');
    const previewImage = document.getElementById('preview-image');
    const previewContainer = document.getElementById('preview-container');

    if (!preview || !previewImage) return;

    preview.style.display = 'block';

    // 检查是否是图片格式
    if (iconValue.startsWith('[img:') || iconValue.startsWith('[favicon:')) {
        // 图片 URL
        const imgUrl = unwrapIconValue(iconValue);
        const previewUrl = proxiedIconUrl(imgUrl);
        previewImage.src = previewUrl;
        previewImage.style.display = 'block';
        previewContainer.innerHTML = `<img src="${escapeIconAttr(previewUrl)}" alt="预览" style="width: 40px; height: 40px; border-radius: 3px; border: 1px solid var(--border-color); object-fit: contain;" onerror="this.parentElement.innerHTML='<span style=\\'font-size: 32px;\\'>🌐</span>'">`;
    } else if (isImageIconUrl(iconValue)) {
        const previewUrl = proxiedIconUrl(iconValue);
        previewImage.src = previewUrl;
        previewImage.style.display = 'block';
        previewContainer.innerHTML = `<img src="${escapeIconAttr(previewUrl)}" alt="预览" style="width: 40px; height: 40px; border-radius: 3px; border: 1px solid var(--border-color); object-fit: contain;" onerror="this.parentElement.innerHTML='<span style=\\'font-size: 32px;\\'>🌐</span>'">`;
    } else {
        // Emoji
        previewImage.style.display = 'none';
        previewContainer.innerHTML = `<span style="font-size: 32px;">${escapeHtml(iconValue || '🌐')}</span>`;
    }
}
