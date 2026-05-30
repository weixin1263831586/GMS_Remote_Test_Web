#!/usr/bin/env python3
"""
异步日志流管理器 - 解决日志同步问题

使用 FastAPI WebSocket 实现高性能异步日志推送
"""

import asyncio
from collections import deque
from typing import Dict
from datetime import datetime
from fastapi import WebSocket
import logging

logger = logging.getLogger(__name__)


class LogStreamer:
    """
    异步日志流管理器

    特性：
    - 异步非阻塞日志发送
    - 批量推送优化性能
    - 自动队列管理防止内存溢出
    - 多客户端并发支持
    """

    def __init__(self, max_queue_size: int = 10000, batch_size: int = 100, batch_timeout: float = 0.1):
        """
        初始化日志流管理器

        Args:
            max_queue_size: 每个客户端最大队列长度
            batch_size: 批量发送的最大日志条数
            batch_timeout: 批量发送超时时间（秒）
        """
        # 客户端连接 {client_id: WebSocket}
        self.connections: Dict[str, WebSocket] = {}

        # 日志队列 {client_id: deque}
        self.client_queues: Dict[str, deque] = {}

        # 后台发送任务 {client_id: Task}
        self._sender_tasks: Dict[str, asyncio.Task] = {}

        # 配置
        self.max_queue_size = max_queue_size
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout

        # 统计信息
        self.stats = {
            'total_logs_sent': 0,
            'total_clients': 0,
            'errors': 0
        }

    async def connect(self, client_id: str, websocket: WebSocket):
        """
        客户端连接

        Args:
            client_id: 客户端唯一标识
            websocket: WebSocket 连接对象
        """
        await websocket.accept()

        self.connections[client_id] = websocket
        self.client_queues[client_id] = deque()

        # 启动后台发送任务
        self._sender_tasks[client_id] = asyncio.create_task(
            self._log_sender(client_id)
        )

        self.stats['total_clients'] += 1

        logger.info(f"[LogStreamer] Client {client_id} connected")

        # 发送连接确认
        await self._send_direct(client_id, {
            'type': 'connected',
            'client_id': client_id,
            'timestamp': datetime.now().isoformat(),
            'message': 'WebSocket 连接成功'
        })

    async def disconnect(self, client_id: str):
        """
        客户端断开

        Args:
            client_id: 客户端唯一标识
        """
        # 取消发送任务
        if client_id in self._sender_tasks:
            self._sender_tasks[client_id].cancel()
            try:
                await self._sender_tasks[client_id]
            except asyncio.CancelledError:
                pass
            del self._sender_tasks[client_id]

        # 关闭 WebSocket
        if client_id in self.connections:
            try:
                await self.connections[client_id].close()
            except Exception:
                pass
            del self.connections[client_id]

        # 清理队列
        if client_id in self.client_queues:
            del self.client_queues[client_id]

        logger.info(f"[LogStreamer] Client {client_id} disconnected")

    async def emit_log(self, client_id: str, log_message: str, log_type: str = 'info'):
        """
        发送日志（非阻塞，立即返回）

        Args:
            client_id: 客户端 ID
            log_message: 日志内容
            log_type: 日志类型 (info, error, success, warning)
        """
        if client_id not in self.client_queues:
            # 客户端未连接，记录警告
            logger.warning(f"[LogStreamer] Client {client_id} not connected, log dropped: {log_message[:50]}")
            return

        # 将日志放入队列（立即返回，不等待发送）
        log_entry = {
            'log': log_message,
            'type': log_type,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }

        self.client_queues[client_id].append(log_entry)
        self.stats['total_logs_sent'] += 1

        # 限制队列大小，防止内存溢出
        queue_len = len(self.client_queues[client_id])
        if queue_len > self.max_queue_size:
            # 丢弃最旧的日志
            self.client_queues[client_id].popleft()
            logger.warning(f"[LogStreamer] Queue overflow for {client_id}, dropped oldest log (queue size: {queue_len})")

    async def _log_sender(self, client_id: str):
        """
        后台发送任务 - 异步批量发送日志

        Args:
            client_id: 客户端 ID
        """
        websocket = self.connections[client_id]
        queue = self.client_queues[client_id]

        logger.info(f"[LogStreamer] Started log sender for {client_id}")

        try:
            while True:
                try:
                    # 批量收集日志
                    batch = []
                    deadline = asyncio.get_event_loop().time() + self.batch_timeout

                    # 收集最多 batch_size 条日志，或等待超时
                    while len(batch) < self.batch_size and asyncio.get_event_loop().time() < deadline:
                        if queue:
                            batch.append(queue.popleft())
                        else:
                            # 队列空时短暂休眠
                            await asyncio.sleep(0.01)

                    # 发送批量日志
                    if batch:
                        await websocket.send_json({
                            'type': 'log_batch',
                            'logs': batch,
                            'count': len(batch)
                        })

                except Exception as e:
                    logger.error(f"[LogStreamer] Error sending logs to {client_id}: {e}")
                    self.stats['errors'] += 1
                    await asyncio.sleep(1)  # 出错时等待 1 秒

        except asyncio.CancelledError:
            logger.info(f"[LogStreamer] Log sender for {client_id} cancelled")
            raise

    async def _send_direct(self, client_id: str, data: dict):
        """
        直接发送单条消息（不经过队列）

        Args:
            client_id: 客户端 ID
            data: 要发送的数据
        """
        if client_id in self.connections:
            try:
                await self.connections[client_id].send_json(data)
            except Exception as e:
                logger.error(f"[LogStreamer] Error sending direct message to {client_id}: {e}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            'active_clients': len(self.connections),
            'queues': {
                cid: len(q) for cid, q in self.client_queues.items()
            }
        }

    def is_connected(self, client_id: str) -> bool:
        """检查客户端是否连接"""
        return client_id in self.connections


# 全局日志流管理器实例
log_streamer = LogStreamer()
