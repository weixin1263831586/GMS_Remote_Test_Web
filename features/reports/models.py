"""
GMS测试报告分析器 - 统一的报告解析模块
整合了XML解析、文件处理和分析功能
"""

import logging
from dataclasses import dataclass


logger = logging.getLogger(__name__)

# 优先使用lxml,如果不可用则回退到ElementTree
try:
    from lxml import etree
    USE_LXML = True
    logger.info("使用lxml进行XML解析(高性能模式)")
except ImportError:
    import xml.etree.ElementTree as ET
    USE_LXML = False
    logger.warning("lxml不可用,使用ElementTree(标准库模式)")


@dataclass
class TestFailure:
    """测试失败信息数据类"""
    name: str
    reason: str
    module: str = '未知模块'
    stack_trace: str = ''


@dataclass
class TestReport:
    """测试报告数据类"""
    test_type: str
    device: str
    suite_version: str      # 测试套件版本（如 16.1_r2）
    android_version: str    # Android版本（从 build_version_release 获取）
    start_time: str
    total: int
    pass_count: int
    fail_count: int
    pass_rate: str
    failures: list[TestFailure]


# 复用容错 XML 解析器，跳过报告中的非法实体和控制字符。
# 报告 XML 产自设备与套件运行环境（不可信证据，见仓库安全边界约定）：
# - resolve_entities=False / no_network=True：阻断 XXE 与外部实体拉取，
#   实体引用原样保留而非展开求值；
# - huge_tree=True：真实 test_result.xml 可达 1.5GB（周报解析路径依赖），
#   实体既不展开，放大攻击面也就随之关闭。
_LXML_PARSER = (
    etree.XMLParser(
        remove_blank_text=True,
        huge_tree=True,
        recover=True,
        resolve_entities=False,
        no_network=True,
    )
    if USE_LXML
    else None
)



if USE_LXML:
    ET = None
