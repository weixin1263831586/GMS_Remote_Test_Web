"""不可信报告 XML 的实体解析安全门（XXE 回归）。

测试报告（test_result.xml 等）产自设备与套件运行环境，属不可信证据。
``_LXML_PARSER`` 必须以 ``resolve_entities=False, no_network=True`` 构建：
外部实体（XXE）与实体展开放大（billion laughs）都不允许在解析路径上
发生。lxml 不可用时标准库 ElementTree 天然不解析外部实体，同一测试
同样必须通过。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from features.reports.models import USE_LXML
from features.reports.xml_parser import XMLReportParser


XXE_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE result ['
    '<!ENTITY xxe SYSTEM "file:///etc/hostname">'
    ']>'
    '<result suiteVersion="16.1" device="serial" startTime="2026-01-01">'
    '<Module name="m"/><Test summary="pass" result="pass" &xxe;="/>'
    '</result>'
)

BILLION_LAUGHS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE result ['
    '<!ENTITY a0 "0123456789012345678901234567890123456789">'
    '<!ENTITY a1 "&a0;&a0;&a0;&a0;&a0;&a0;&a0;&a0;&a0;&a0;">'
    '<!ENTITY a2 "&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;&a1;">'
    '<!ENTITY a3 "&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;&a2;">'
    '<!ENTITY a4 "&a3;&a3;&a3;&a3;&a3;&a3;&a3;&a3;&a3;&a3;">'
    '<!ENTITY a5 "&a4;&a4;&a4;&a4;&a4;&a4;&a4;&a4;&a4;&a4;">'
    ']>'
    '<result suiteVersion="16.1" device="serial" startTime="2026-01-01">'
    '<Module name="m"/>&a5;</result>'
)

BENIGN = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<result suiteVersion="16.1" device="serial" startTime="2026-01-01">'
    '<Module name="m"/><Test summary="pass" result="pass"/>'
    '</result>'
)


class XxeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.parser = XMLReportParser()

    def test_external_entity_not_resolved(self):
        """XXE 载荷不得读出文件内容：要么解析失败返回 None，
        要么实体保持未展开状态、属性里看不到 /etc/hostname 的内容。"""

        report = self.parser.parse_content(XXE_PAYLOAD)
        if report is None:
            return  # 解析器拒绝携带 DTD 实体的文档，同样合格
        serialized = repr(report)
        hostname_file = Path("/etc/hostname")
        leaked = hostname_file.is_file() and hostname_file.read_text(
            encoding="utf-8", errors="ignore"
        ).strip() in serialized
        self.assertFalse(leaked, "外部实体内容泄漏进了解析结果")

    def test_billion_laughs_not_expanded(self):
        """嵌套实体不得被展开放大（huge_tree 也不能成为放大器）。"""

        report = self.parser.parse_content(BILLION_LAUGHS)
        if report is None:
            return
        # 展开成功的标志是异常巨大的文本体积；未展开时 report 字段全为
        # 固定小字符串。
        for field in vars(report).values():
            if isinstance(field, str):
                self.assertLess(
                    len(field), 10_000, "嵌套实体被展开成超大文本"
                )

    def test_benign_report_still_parses(self):
        """加固不能误伤正常报告。"""

        report = self.parser.parse_content(BENIGN)
        self.assertIsNotNone(report)

    def test_parser_flags_present_when_lxml(self):
        """lxml 路径必须显式禁用实体展开（防手滑回退）。

        lxml 的 XMLParser 不暴露 resolve_entities 属性，用行为验证：
        内部实体引用必须原样保留，而不是被展开成实体内容。
        """

        if not USE_LXML:
            self.skipTest("lxml 不可用，走 ElementTree 路径")
        from lxml import etree as lxml_etree

        from features.reports.models import _LXML_PARSER

        document = (
            '<?xml version="1.0"?><!DOCTYPE r ['
            '<!ENTITY inner "EXPANDED-SECRET">]>'
            "<r>&inner;</r>"
        )
        root = lxml_etree.fromstring(document.encode("utf-8"), _LXML_PARSER)
        self.assertNotIn(
            "EXPANDED-SECRET",
            (root.text or "") + (root.tail or ""),
            "内部实体被展开——resolve_entities 未关闭",
        )


if __name__ == "__main__":
    unittest.main()
