#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网站图标获取器 - Web优化版本
支持从多种来源获取网站的真实图标，优化了Web环境的使用
"""

import asyncio
import aiohttp
import re
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Any
import logging
from dataclasses import dataclass
import json
import os
import glob
import hashlib
import mimetypes
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@dataclass
class IconResult:
    """图标获取结果"""
    success: bool
    icon_url: str = ""
    icon_type: str = ""  # svg, ico, png, emoji
    source: str = ""     # html, root, api, cache, predefined
    size: int = 0        # 图标尺寸
    error: str = ""
    cache_key: str = ""  # 缓存键
    original_icon_url: str = ""  # 原始远程图标地址

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'success': self.success,
            'icon_url': self.icon_url,
            'icon_type': self.icon_type,
            'source': self.source,
            'size': self.size,
            'error': self.error,
            'cache_key': self.cache_key,
            'original_icon_url': self.original_icon_url
        }


class IconFetcher:
    """网站图标获取器 - Web优化版本"""

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 常量配置
    ICON_VALIDATION_TIMEOUT = 5  # 图标验证超时时间（秒）
    MAX_ICON_CANDIDATES = 5  # 最大图标候选数量
    IMAGE_CHUNK_SIZE = 1024  # 图片内容检查大小（字节）
    MAX_ICON_DOWNLOAD_BYTES = 512 * 1024  # 最大图标下载大小，避免误缓存大图

    # 预定义的常见网站图标映射
    PREDEFINED_ICONS = {
        'github.com': {'icon': '📦', 'type': 'emoji'},
        'gitlab.com': {'icon': '🦊', 'type': 'emoji'},
        'google.com': {'icon': '🔍', 'type': 'emoji'},
        'youtube.com': {'icon': '▶️', 'type': 'emoji'},
        'twitter.com': {'icon': '🐦', 'type': 'emoji'},
        'facebook.com': {'icon': '📘', 'type': 'emoji'},
        'linkedin.com': {'icon': '💼', 'type': 'emoji'},
        'reddit.com': {'icon': '🤖', 'type': 'emoji'},
        'stackoverflow.com': {'icon': '📚', 'type': 'emoji'},
        'wikipedia.org': {'icon': '📖', 'type': 'emoji'},
        'amazon.com': {'icon': '📦', 'type': 'emoji'},
        'netflix.com': {'icon': '🎬', 'type': 'emoji'},
        'spotify.com': {'icon': '🎵', 'type': 'emoji'},
        'slack.com': {'icon': '💬', 'type': 'emoji'},
        'microsoft.com': {'icon': '🪟', 'type': 'emoji'},
        'apple.com': {'icon': '🍎', 'type': 'emoji'},
        'npmjs.com': {'icon': '📦', 'type': 'emoji'},
        'docker.com': {'icon': '🐳', 'type': 'emoji'},
        'kubernetes.io': {'icon': '☸️', 'type': 'emoji'},
        'redis.io': {'icon': '🔴', 'type': 'emoji'},
        'mongodb.com': {'icon': '🍃', 'type': 'emoji'},
        'mysql.com': {'icon': '🐬', 'type': 'emoji'},
        'postgresql.org': {'icon': '🐘', 'type': 'emoji'},
        'nginx.org': {'icon': '🍀', 'type': 'emoji'},
        'apache.org': {'icon': '🪶', 'type': 'emoji'},
        'python.org': {'icon': '🐍', 'type': 'emoji'},
        'java.com': {'icon': '☕', 'type': 'emoji'},
        'golang.org': {'icon': '🐹', 'type': 'emoji'},
        'rust-lang.org': {'icon': '🦀', 'type': 'emoji'},
        'grafana.com': {'icon': '📊', 'type': 'emoji'},
        'prometheus.io': {'icon': '🔥', 'type': 'emoji'},
        'jenkins.io': {'icon': '👷', 'type': 'emoji'},
        'android.com': {'icon': '🤖', 'type': 'emoji'},
        'aws.amazon.com': {'icon': '☁️', 'type': 'emoji'},
        'azure.microsoft.com': {'icon': '🌐', 'type': 'emoji'},
        'digitalocean.com': {'icon': '🌊', 'type': 'emoji'},
    }

    # 图标缓存文件路径
    CACHE_FILE = os.path.join(BASE_DIR, "data", "icon_cache.json")
    LOCAL_ICON_DIR = os.path.join(BASE_DIR, "static", "icons", "favicons")
    LOCAL_ICON_URL_PREFIX = "/static/icons/favicons"
    DEFAULT_ICON_URL = "/static/icons/site-default.svg"
    CACHE_DURATION = timedelta(days=7)  # 缓存7天
    MAX_CACHE_SIZE = 1000  # 最大缓存条目数

    def __init__(self, timeout: int = 10, use_cache: bool = True):
        self.timeout = timeout
        self.use_cache = use_cache
        self.session = None
        self.cache = self._load_cache()

    async def get_session(self):
        """获取aiohttp会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            }
            self.session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self.session

    async def close(self):
        """关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    def _load_cache(self) -> Dict[str, Any]:
        """加载图标缓存"""
        if not self.use_cache:
            return {}

        try:
            # 直接尝试打开文件，避免TOCTOU问题
            with open(self.CACHE_FILE, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 清理过期缓存
            current_time = datetime.now()
            valid_cache = {}

            for key, value in cache_data.items():
                try:
                    cache_time = datetime.fromisoformat(value.get('timestamp', ''))
                    if current_time - cache_time < self.CACHE_DURATION:
                        valid_cache[key] = value
                except Exception:
                    continue

            return valid_cache
        except FileNotFoundError:
            # 文件不存在是正常情况，不需要警告
            return {}
        except Exception as e:
            logger.warning(f"加载图标缓存失败: {e}")
            return {}

    def _save_cache(self, new_entries: Dict[str, Any] = None):
        """保存图标缓存

        Args:
            new_entries: 只保存新条目，避免不必要的重写
        """
        if not self.use_cache:
            return

        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.CACHE_FILE), exist_ok=True)

            # 只更新新条目的时间戳，避免不必要的操作
            if new_entries:
                current_time = datetime.now().isoformat()
                for key in new_entries:
                    if key in self.cache:
                        self.cache[key]['timestamp'] = current_time

            # 实施LRU策略：如果缓存超过最大大小，删除最旧的条目
            if len(self.cache) > self.MAX_CACHE_SIZE:
                # 按时间戳排序，删除最旧的条目
                sorted_items = sorted(
                    self.cache.items(),
                    key=lambda x: x[1].get('timestamp', '')
                )
                # 保留最新的MAX_CACHE_SIZE个条目
                self.cache = dict(sorted_items[-self.MAX_CACHE_SIZE:])
                logger.info(f"缓存已满，删除了 {len(sorted_items) - self.MAX_CACHE_SIZE} 个旧条目")

            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.warning(f"保存图标缓存失败: {e}")

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return {
            'size': len(self.cache),
            'keys': list(self.cache.keys())
        }

    def clear_cache(self):
        """清理缓存"""
        self.cache.clear()
        try:
            if os.path.exists(self.CACHE_FILE):
                os.remove(self.CACHE_FILE)
        except Exception as e:
            logger.warning(f"删除缓存文件失败: {e}")

    def _cache_result(self, cache_key: str, result: IconResult) -> None:
        """缓存结果并持久化"""
        self.cache[cache_key] = result.to_dict()
        self._save_cache({cache_key: self.cache[cache_key]})

    @classmethod
    def is_remote_url(cls, value: str) -> bool:
        """判断是否为远程HTTP(S) URL"""
        return isinstance(value, str) and value.lower().startswith(('http://', 'https://'))

    @classmethod
    def is_local_static_url(cls, value: str) -> bool:
        """判断是否为本地静态图标 URL"""
        return isinstance(value, str) and (
            value.startswith(cls.LOCAL_ICON_URL_PREFIX + '/') or value == cls.DEFAULT_ICON_URL
        )

    @classmethod
    def static_url_to_path(cls, url: str) -> str:
        """将本地静态图标 URL 转为文件路径"""
        if url == cls.DEFAULT_ICON_URL:
            return os.path.join(cls.BASE_DIR, url.lstrip('/'))
        if not isinstance(url, str) or not url.startswith(cls.LOCAL_ICON_URL_PREFIX + '/'):
            return ""

        filename = os.path.basename(urlparse(url).path)
        if not filename:
            return ""
        return os.path.join(cls.LOCAL_ICON_DIR, filename)

    @classmethod
    def default_icon_path(cls) -> str:
        """默认图标文件路径"""
        return os.path.join(cls.BASE_DIR, cls.DEFAULT_ICON_URL.lstrip('/'))

    def _local_url_for_path(self, path: str) -> str:
        """本地文件路径转 URL"""
        return f"{self.LOCAL_ICON_URL_PREFIX}/{os.path.basename(path)}"

    def _local_cache_key(self, icon_url: str) -> str:
        return f"iconfile:{hashlib.sha256(icon_url.encode('utf-8')).hexdigest()}"

    def _icon_hash(self, icon_url: str) -> str:
        return hashlib.sha256(icon_url.encode('utf-8')).hexdigest()

    def _find_existing_local_icon(self, icon_url: str) -> str:
        """查找远程图标是否已下载到本地"""
        icon_hash = self._icon_hash(icon_url)
        for path in glob.glob(os.path.join(self.LOCAL_ICON_DIR, f"{icon_hash}.*")):
            if os.path.isfile(path):
                return self._local_url_for_path(path)
        return ""

    def _extension_from_response(self, icon_url: str, content_type: str, data: bytes) -> str:
        """根据响应和文件头推断图标扩展名"""
        content_type = (content_type or '').split(';', 1)[0].strip().lower()
        content_type_map = {
            'image/svg+xml': '.svg',
            'image/png': '.png',
            'image/jpeg': '.jpg',
            'image/jpg': '.jpg',
            'image/gif': '.gif',
            'image/webp': '.webp',
            'image/x-icon': '.ico',
            'image/vnd.microsoft.icon': '.ico',
        }
        if content_type in content_type_map:
            return content_type_map[content_type]

        guessed = mimetypes.guess_extension(content_type) if content_type else None
        if guessed in {'.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico'}:
            return '.jpg' if guessed == '.jpeg' else guessed

        path_ext = os.path.splitext(urlparse(icon_url).path)[1].lower()
        if path_ext in {'.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico'}:
            return '.jpg' if path_ext == '.jpeg' else path_ext

        if data[:4] == b'\x89PNG':
            return '.png'
        if data[:2] == b'\xff\xd8':
            return '.jpg'
        if data[:4] == b'GIF8':
            return '.gif'
        if data[:4].lower().startswith(b'<svg') or b'<svg' in data[:200].lower():
            return '.svg'
        if data[:4] == b'\x00\x00\x01\x00':
            return '.ico'

        return '.ico'

    def _icon_type_from_extension(self, ext: str) -> str:
        return ext.lstrip('.').lower() or 'unknown'

    async def _download_icon_to_local(self, icon_url: str, source: str = "download") -> IconResult:
        """下载远程图标到本地静态目录"""
        if not self.is_remote_url(icon_url):
            return IconResult(success=False, error="不是远程图标URL")

        existing_url = self._find_existing_local_icon(icon_url)
        if existing_url:
            return IconResult(
                success=True,
                icon_url=existing_url,
                icon_type=os.path.splitext(existing_url)[1].lstrip('.') or 'unknown',
                source='local_cache',
                original_icon_url=icon_url
            )

        try:
            session = await self.get_session()
            async with session.get(
                icon_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=min(self.timeout, self.ICON_VALIDATION_TIMEOUT))
            ) as response:
                if response.status != 200:
                    return IconResult(success=False, error=f"HTTP {response.status}")

                content_type = response.headers.get('Content-Type', '')
                content_length = response.headers.get('Content-Length')
                if content_length:
                    try:
                        if int(content_length) > self.MAX_ICON_DOWNLOAD_BYTES:
                            return IconResult(success=False, error="图标文件过大")
                    except ValueError:
                        pass

                chunks = []
                total_size = 0
                async for chunk in response.content.iter_chunked(8192):
                    total_size += len(chunk)
                    if total_size > self.MAX_ICON_DOWNLOAD_BYTES:
                        return IconResult(success=False, error="图标文件过大")
                    chunks.append(chunk)

                data = b''.join(chunks)
                if not data:
                    return IconResult(success=False, error="图标内容为空")

                content_type_lower = content_type.lower()
                is_image_type = any(marker in content_type_lower for marker in ['image/', 'svg'])
                if not is_image_type and not self._is_image_content(data):
                    return IconResult(success=False, error="响应不是图片")

                ext = self._extension_from_response(str(response.url), content_type, data)
                filename = f"{self._icon_hash(icon_url)}{ext}"
                os.makedirs(self.LOCAL_ICON_DIR, exist_ok=True)
                local_path = os.path.join(self.LOCAL_ICON_DIR, filename)

                if not os.path.exists(local_path):
                    with open(local_path, 'wb') as f:
                        f.write(data)

                return IconResult(
                    success=True,
                    icon_url=self._local_url_for_path(local_path),
                    icon_type=self._icon_type_from_extension(ext),
                    source=source,
                    size=len(data),
                    original_icon_url=icon_url
                )

        except Exception as e:
            logger.debug(f"下载图标到本地失败 {icon_url}: {e}")
            return IconResult(success=False, error=str(e), original_icon_url=icon_url)

    async def localize_icon_url(self, icon_url: str) -> IconResult:
        """把任意图标值转换为本地可稳定访问的图标。"""
        if not icon_url or not isinstance(icon_url, str):
            return IconResult(success=False, error="图标URL为空", icon_url=self.DEFAULT_ICON_URL, icon_type='svg')

        if not self.is_remote_url(icon_url):
            return IconResult(success=True, icon_url=icon_url, icon_type='local' if self.is_local_static_url(icon_url) else 'emoji', source='local')

        cache_key = self._local_cache_key(icon_url)
        cached = self.cache.get(cache_key)
        if cached:
            cached_icon = cached.get('icon_url', '')
            cached_path = self.static_url_to_path(cached_icon)
            if cached_path and os.path.exists(cached_path):
                return IconResult(
                    success=True,
                    icon_url=cached_icon,
                    icon_type=cached.get('icon_type', 'unknown'),
                    source='local_cache',
                    size=cached.get('size', 0),
                    original_icon_url=icon_url,
                    cache_key=cache_key
                )

        downloaded = await self._download_icon_to_local(icon_url)
        if downloaded.success:
            downloaded.cache_key = cache_key
            self._cache_result(cache_key, downloaded)
            return downloaded

        existing_url = self._find_existing_local_icon(icon_url)
        if existing_url:
            return IconResult(
                success=True,
                icon_url=existing_url,
                icon_type=os.path.splitext(existing_url)[1].lstrip('.') or 'unknown',
                source='local_cache',
                original_icon_url=icon_url,
                cache_key=cache_key
            )

        return IconResult(
            success=False,
            icon_url=self.DEFAULT_ICON_URL,
            icon_type='svg',
            source='fallback',
            error=downloaded.error,
            original_icon_url=icon_url,
            cache_key=cache_key
        )

    async def _localize_result(self, result: IconResult, cache_key: str = "") -> IconResult:
        """将抓取结果中的远程图标落盘为本地图标"""
        if not result.success or not self.is_remote_url(result.icon_url):
            if cache_key:
                result.cache_key = cache_key
            return result

        local_result = await self.localize_icon_url(result.icon_url)
        if local_result.success:
            local_result.source = result.source if result.source.startswith('api_') else f"{result.source}_local"
            local_result.cache_key = cache_key
            return local_result

        local_result.cache_key = cache_key
        return local_result

    async def fetch_icon_async(self, url: str) -> IconResult:
        """
        异步获取网站图标

        Args:
            url: 网站URL

        Returns:
            IconResult对象
        """
        try:
            if not url or not url.strip():
                return IconResult(success=False, error="URL为空")

            # 解析URL
            parsed_url = urlparse(url)
            if not parsed_url.scheme:
                url = 'https://' + url
                parsed_url = urlparse(url)

            domain = parsed_url.netloc
            cache_key = f"favicon:{domain}"

            # 1. 检查缓存
            if cache_key in self.cache:
                cached_data = self.cache[cache_key]
                logger.info(f"使用缓存的图标: {domain}")
                cached_icon_url = cached_data.get('icon_url', '')
                if not (cached_icon_url == self.DEFAULT_ICON_URL and cached_data.get('source') == 'fallback'):
                    if self.is_remote_url(cached_icon_url):
                        localized = await self.localize_icon_url(cached_icon_url)
                        if localized.success:
                            localized.source = 'cache_local'
                            localized.cache_key = cache_key
                            self._cache_result(cache_key, localized)
                            return localized

                        return IconResult(
                            success=True,
                            icon_url=self.DEFAULT_ICON_URL,
                            icon_type='svg',
                            source='fallback',
                            cache_key=cache_key,
                            original_icon_url=cached_icon_url
                        )

                    if self.is_local_static_url(cached_icon_url):
                        cached_path = self.static_url_to_path(cached_icon_url)
                        if cached_path and not os.path.exists(cached_path):
                            original_url = cached_data.get('original_icon_url', '')
                            if self.is_remote_url(original_url):
                                localized = await self.localize_icon_url(original_url)
                                if localized.success:
                                    localized.source = 'cache_local'
                                    localized.cache_key = cache_key
                                    self._cache_result(cache_key, localized)
                                    return localized

                            return IconResult(
                                success=True,
                                icon_url=self.DEFAULT_ICON_URL,
                                icon_type='svg',
                                source='fallback',
                                cache_key=cache_key,
                                original_icon_url=original_url
                            )

                    return IconResult(
                        success=True,
                        icon_url=cached_icon_url,
                        icon_type=cached_data.get('icon_type', ''),
                        source='cache',
                        cache_key=cache_key,
                        original_icon_url=cached_data.get('original_icon_url', '')
                    )

                logger.info(f"忽略默认兜底缓存并重新尝试获取图标: {domain}")

            # 2. 检查预定义图标
            if domain in self.PREDEFINED_ICONS:
                predefined = self.PREDEFINED_ICONS[domain]
                logger.info(f"使用预定义图标: {domain}")

                result = IconResult(
                    success=True,
                    icon_url=predefined['icon'],
                    icon_type=predefined['type'],
                    source='predefined',
                    cache_key=cache_key
                )

                self._cache_result(cache_key, result)
                return result

            # 3. 从HTML获取图标
            html_result = await self._fetch_from_html(url)
            if html_result.success:
                localized = await self._localize_result(html_result, cache_key)
                if localized.success:
                    self._cache_result(cache_key, localized)
                    return localized

            # 4. 从根目录获取图标
            root_result = await self._fetch_from_root(url)
            if root_result.success:
                localized = await self._localize_result(root_result, cache_key)
                if localized.success:
                    self._cache_result(cache_key, localized)
                    return localized

            # 5. 使用第三方API
            api_result = await self._fetch_from_api(domain)
            if api_result.success:
                localized = await self._localize_result(api_result, cache_key)
                if localized.success:
                    self._cache_result(cache_key, localized)
                    return localized

            # 都失败了，返回默认图标
            fallback = IconResult(
                success=True,
                error="无法获取网站图标",
                icon_url=self.DEFAULT_ICON_URL,
                icon_type="svg",
                source="fallback",
                cache_key=cache_key
            )
            return fallback

        except Exception as e:
            logger.error(f"获取图标时出错: {e}")
            return IconResult(success=False, error=str(e))

    async def batch_fetch_icons_async(self, urls: List[str]) -> List[Dict[str, Any]]:
        """批量获取图标"""
        tasks = [self.fetch_icon_async(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        formatted_results = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                formatted_results.append({
                    'url': url,
                    'success': False,
                    'error': str(result)
                })
            else:
                result_dict = result.to_dict()
                result_dict['url'] = url
                formatted_results.append(result_dict)

        return formatted_results

    async def _fetch_from_html(self, url: str) -> IconResult:
        """从HTML页面中提取图标链接"""
        try:
            session = await self.get_session()

            async with session.get(url) as response:
                if response.status != 200:
                    return IconResult(success=False, error=f"HTTP {response.status}")

                html = await response.text()

                # 使用正则表达式查找图标链接（比BeautifulSoup更快）
                icon_candidates = []

                # 查找 apple-touch-icon
                apple_icons = re.findall(
                    r'<link[^>]*rel=["\']apple-touch-icon["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
                    html, re.IGNORECASE
                )
                for icon_url in apple_icons:
                    icon_candidates.append({
                        'url': urljoin(url, icon_url),
                        'type': 'png',
                        'priority': 1
                    })

                # 查找 icon
                icons = re.findall(
                    r'<link[^>]*rel=["\']icon["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
                    html, re.IGNORECASE
                )
                for icon_url in icons:
                    icon_candidates.append({
                        'url': urljoin(url, icon_url),
                        'type': 'unknown',
                        'priority': 2
                    })

                # 查找 shortcut icon
                shortcut_icons = re.findall(
                    r'<link[^>]*rel=["\']shortcut icon["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
                    html, re.IGNORECASE
                )
                for icon_url in shortcut_icons:
                    icon_candidates.append({
                        'url': urljoin(url, icon_url),
                        'type': 'ico',
                        'priority': 3
                    })

                # 按优先级排序并验证
                icon_candidates.sort(key=lambda x: x['priority'])

                # 并发验证候选图标
                validation_tasks = [
                    self._validate_icon_url(candidate['url'])
                    for candidate in icon_candidates[:self.MAX_ICON_CANDIDATES]
                ]
                validation_results = await asyncio.gather(*validation_tasks, return_exceptions=True)

                for candidate, is_valid in zip(icon_candidates[:self.MAX_ICON_CANDIDATES], validation_results):
                    if is_valid and not isinstance(is_valid, Exception):
                        return IconResult(
                            success=True,
                            icon_url=candidate['url'],
                            icon_type=candidate['type'],
                            source='html'
                        )

                return IconResult(success=False, error="HTML中未找到有效图标")

        except Exception as e:
            logger.debug(f"从HTML获取图标失败: {e}")
            return IconResult(success=False, error=str(e))

    async def _fetch_from_root(self, url: str) -> IconResult:
        """尝试从网站根目录获取常见图标文件"""
        parsed_url = urlparse(url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

        common_paths = [
            ('/favicon.svg', 'svg'),
            ('/favicon.ico', 'ico'),
            ('/favicon.png', 'png'),
            ('/icon.svg', 'svg'),
            ('/icon.png', 'png'),
            ('/apple-touch-icon.png', 'png'),
            ('/assets/favicon.svg', 'svg'),
            ('/static/favicon.svg', 'svg'),
            ('/img/favicon.svg', 'svg'),
        ]

        # 并发验证所有路径
        validation_tasks = [
            self._validate_icon_url(urljoin(base_url, path))
            for path, _ in common_paths
        ]
        validation_results = await asyncio.gather(*validation_tasks, return_exceptions=True)

        for (path, icon_type), is_valid in zip(common_paths, validation_results):
            if is_valid and not isinstance(is_valid, Exception):
                icon_url = urljoin(base_url, path)
                return IconResult(
                    success=True,
                    icon_url=icon_url,
                    icon_type=icon_type,
                    source='root'
                )

        return IconResult(success=False, error="根目录未找到图标文件")

    async def _fetch_from_api(self, domain: str) -> IconResult:
        """使用第三方API获取图标"""
        api_services = [
            ('https://www.google.com/s2/favicons?domain=' + domain + '&sz=128', 'google'),
            ('https://icons.duckduckgo.com/ip3/' + domain + '.ico', 'duckduckgo'),
            ('https://www.google.com/s2/favicons?domain=' + domain + '&sz=64', 'google_small'),
        ]

        for api_url, service_name in api_services:
            if await self._validate_icon_url(api_url):
                return IconResult(
                    success=True,
                    icon_url=api_url,
                    icon_type='api',
                    source=f'api_{service_name}'
                )

        return IconResult(success=False, error="API服务不可用")

    async def _validate_icon_url(self, url: str) -> bool:
        """验证图标URL是否可访问"""
        try:
            session = await self.get_session()

            # 先尝试HEAD请求
            try:
                async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=self.ICON_VALIDATION_TIMEOUT)) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if any(ct in content_type.lower() for ct in ['image/', 'svg', 'octet-stream']):
                            return True
            except Exception:
                pass

            # HEAD失败，尝试GET请求
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.ICON_VALIDATION_TIMEOUT)) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if any(ct in content_type.lower() for ct in ['image/', 'svg']):
                            return True

                        # 检查文件头
                        chunk = await response.content.read(self.IMAGE_CHUNK_SIZE)
                        if self._is_image_content(chunk):
                            return True
            except Exception:
                pass

            return False

        except Exception:
            return False

    @staticmethod
    def _is_image_content(data: bytes) -> bool:
        """检查是否是图片内容"""
        if len(data) < 4:
            return False

        # 检查常见图片格式的文件头
        if data[:4] == b'\x89PNG':  # PNG
            return True
        if data[:2] == b'\xff\xd8':  # JPEG
            return True
        if data[:4] == b'GIF8':  # GIF
            return True
        if data[:2] == b'BM':  # BMP
            return True
        if b'<svg' in data[:100].lower():  # SVG
            return True

        return False


@asynccontextmanager
async def get_icon_fetcher(timeout: int = 10, use_cache: bool = True):
    """
    IconFetcher异步上下文管理器

    使用方式:
        async with get_icon_fetcher(timeout=10) as fetcher:
            result = await fetcher.fetch_icon_async(url)
    """
    fetcher = IconFetcher(timeout=timeout, use_cache=use_cache)
    try:
        yield fetcher
    finally:
        await fetcher.close()


# 便捷函数
async def fetch_website_icon(url: str, timeout: int = 10) -> Dict[str, Any]:
    """
    获取网站图标的便捷函数

    Args:
        url: 网站URL
        timeout: 超时时间（秒）

    Returns:
        包含图标信息的字典
    """
    fetcher = IconFetcher(timeout=timeout)
    try:
        result = await fetcher.fetch_icon_async(url)
        return result.to_dict()
    finally:
        await fetcher.close()


# 测试函数
async def test_icon_fetcher():
    """测试图标获取功能"""
    test_urls = [
        'https://www.google.com',
        'https://github.com',
        'https://www.python.org',
        'https://deepseek.com',
        'https://www.rock-chips.com',  # Rockchip官网
    ]

    print("=== 图标获取测试 ===\n")

    fetcher = IconFetcher()
    try:
        for url in test_urls:
            print(f"测试: {url}")
            result = await fetcher.fetch_icon_async(url)

            if result.success:
                print(f"✅ 成功: {result.icon_url}")
                print(f"   类型: {result.icon_type}, 来源: {result.source}")
            else:
                print(f"❌ 失败: {result.error}")
            print()
    finally:
        await fetcher.close()


if __name__ == '__main__':
    # 运行测试
    asyncio.run(test_icon_fetcher())
