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
from typing import Optional, List, Dict, Any
import logging
from dataclasses import dataclass
import json
import os
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

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'success': self.success,
            'icon_url': self.icon_url,
            'icon_type': self.icon_type,
            'source': self.source,
            'size': self.size,
            'error': self.error,
            'cache_key': self.cache_key
        }


class IconFetcher:
    """网站图标获取器 - Web优化版本"""

    # 常量配置
    ICON_VALIDATION_TIMEOUT = 5  # 图标验证超时时间（秒）
    MAX_ICON_CANDIDATES = 5  # 最大图标候选数量
    IMAGE_CHUNK_SIZE = 1024  # 图片内容检查大小（字节）

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
    CACHE_FILE = "data/icon_cache.json"
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
                cache_time = datetime.fromisoformat(value.get('timestamp', ''))
                if current_time - cache_time < self.CACHE_DURATION:
                    valid_cache[key] = value

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
                return IconResult(
                    success=True,
                    icon_url=cached_data['icon_url'],
                    icon_type=cached_data['icon_type'],
                    source='cache',
                    cache_key=cache_key
                )

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
                self._cache_result(cache_key, html_result)
                return html_result

            # 4. 从根目录获取图标
            root_result = await self._fetch_from_root(url)
            if root_result.success:
                self._cache_result(cache_key, root_result)
                return root_result

            # 5. 使用第三方API
            api_result = await self._fetch_from_api(domain)
            if api_result.success:
                self._cache_result(cache_key, api_result)
                return api_result

            # 都失败了，返回默认图标
            return IconResult(
                success=False,
                error="无法获取网站图标",
                icon_url="🌐",
                icon_type="emoji"
            )

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