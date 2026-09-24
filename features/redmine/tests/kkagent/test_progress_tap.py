"""ProgressTap：stream-json 事件 → 进度 sink 的分流契约。

重点覆盖 session 分流（实时会话 tail）：kkagent 流一输出
session_id 就转发给 sink.session_available()，只发一次；sink 不支持该
方法时静默跳过，不影响 tool_call/tool_result 分流。
"""

import unittest

from features.redmine.kkagent.progress_tap import ProgressTap


class _Sink:
    def __init__(self):
        self.started = []
        self.finished = []
        self.progress = []
        self.sessions = []

    def tool_started(self, tool_name, tool_input=None, *, stage="kkagent"):
        self.started.append((tool_name, tool_input))

    def tool_finished(self, tool_name, tool_input=None, *, ok, stage="kkagent",
                      duration_ms=None, failure_kind=""):
        self.finished.append((tool_name, ok))

    def progress(self, summary):
        self.progress.append(summary)

    def session_available(self, session_id):
        self.sessions.append(session_id)


class _NoSessionSink:
    """不支持 session_available 的旧 sink：不得因缺少方法而抛错。"""

    def __init__(self):
        self.started = []

    def tool_started(self, tool_name, tool_input=None, *, stage="kkagent"):
        self.started.append(tool_name)


class ProgressTapTests(unittest.TestCase):
    def test_session_forwarded_once(self):
        sink = _Sink()
        tap = ProgressTap(sink)
        tap.on_event({"type": "session", "session_id": "sess-1"})
        tap.on_event({"type": "session", "session_id": "sess-1"})
        tap.on_event({"type": "session", "session_id": "sess-2"})
        self.assertEqual(sink.sessions, ["sess-1"])

    def test_empty_session_id_ignored(self):
        sink = _Sink()
        ProgressTap(sink).on_event({"type": "session", "session_id": ""})
        self.assertEqual(sink.sessions, [])

    def test_tool_events_still_forwarded(self):
        sink = _Sink()
        tap = ProgressTap(sink)
        tap.on_event({"type": "tool_call", "tool_call_id": "c1", "tool_name": "gms_rt_devices_list"})
        tap.on_event({"type": "tool_result", "tool_call_id": "c1", "tool_name": "gms_rt_devices_list"})
        self.assertEqual(sink.started, [("gms_rt_devices_list", None)])
        self.assertEqual(sink.finished, [("gms_rt_devices_list", True)])

    def test_sink_without_session_method_is_tolerated(self):
        sink = _NoSessionSink()
        tap = ProgressTap(sink)
        tap.on_event({"type": "session", "session_id": "sess-x"})  # 不抛错
        tap.on_event({"type": "tool_call", "tool_call_id": "c1", "tool_name": "t"})
        self.assertEqual(sink.started, ["t"])

    def test_none_sink_is_noop(self):
        ProgressTap(None).on_event({"type": "session", "session_id": "s"})


if __name__ == "__main__":
    unittest.main()
