"""
GMS测试报告分析器 - 统一的报告解析模块
整合了XML解析、文件处理和分析功能
"""

import logging
import re
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


def get_opengrok_project_for_android_version(android_version: str, opengrok_config: dict) -> str:
    """返回 Android 主版本对应的 OpenGrok 项目。"""
    if not android_version or not opengrok_config:
        return opengrok_config.get('default_project', 'Android16')

    # 提取Android版本的主版本号
    match = re.match(r'(\d+)', android_version)
    if match:
        major_version = match.group(1)
        project_mapping = opengrok_config.get('project_mapping', {})

        # 根据主版本号查找对应项目
        if major_version in project_mapping:
            return project_mapping[major_version]

    # 如果无法匹配，返回默认项目
    return opengrok_config.get('default_project', 'Android16')


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
_LXML_PARSER = etree.XMLParser(remove_blank_text=True, huge_tree=True, recover=True) if USE_LXML else None



if USE_LXML:
    ET = None
