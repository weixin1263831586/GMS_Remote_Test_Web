"""Bound TLS close-handshake waits without cancelling active HTTP requests.

Uvicorn waits for every protocol's connection_lost callback before lifespan
shutdown. uvloop can wait 30 seconds for a TLS peer's close_notify even after
the response has finished. Only completed, closing HTTPS connections with an
empty application write buffer are eligible for the shorter wait below.
"""

from __future__ import annotations

from uvicorn.protocols.http.auto import AutoHTTPProtocol


TLS_CLOSE_WAIT_SECONDS = 2.0


class ShutdownHttpProtocol(AutoHTTPProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown_requested = False
        self._tls_close_handle = None

    def shutdown(self):
        self._shutdown_requested = True
        if self.cycle is not None:
            # scope 标记由服务端写入（headers/body 均不可信），长轮询据此返回。
            self.cycle.scope["gms.shutdown_requested"] = True
        super().shutdown()
        self._schedule_tls_close()

    def on_response_complete(self):
        super().on_response_complete()
        if self._shutdown_requested:
            self._schedule_tls_close()

    def _schedule_tls_close(self):
        if (self._tls_close_handle is None
                and self.transport.get_extra_info("ssl_object") is not None
                and (self.cycle is None or self.cycle.response_complete)):
            self._tls_close_handle = self.loop.call_later(
                TLS_CLOSE_WAIT_SECONDS, self._close_completed_tls,
            )

    def _close_completed_tls(self):
        self._tls_close_handle = None
        if self not in self.connections or not self.transport.is_closing():
            return
        if self.cycle is not None and not self.cycle.response_complete:
            return
        if self.transport.get_write_buffer_size():
            # 慢读者仍需响应字节，继续等待排空。
            self._tls_close_handle = self.loop.call_later(0.1, self._close_completed_tls)
            return
        self.logger.info("Closing completed HTTPS connection after TLS shutdown wait")
        self.transport.abort()

    def connection_lost(self, exc):
        if self._tls_close_handle is not None:
            self._tls_close_handle.cancel()
            self._tls_close_handle = None
        super().connection_lost(exc)
