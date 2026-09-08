"""Backward-compatible re-export of the shared SSH execution primitive.

SSH 执行的唯一实现已下沉到 :mod:`foundation.ssh_executor`（Build /
System / Cluster 共用；feature 之间禁止互相 import 内部模块）。
本模块保留历史 import 路径，等价于::

    from foundation.ssh_executor import SSHExecutor, ssh_executor
"""

from foundation.ssh_executor import SSHExecutor, ssh_executor


__all__ = ["SSHExecutor", "ssh_executor"]
