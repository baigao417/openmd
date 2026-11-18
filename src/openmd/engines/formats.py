"""Generate the document capability list from routing declarations."""
from __future__ import annotations

import argparse
from pathlib import Path


def render_formats() -> str:
    from openmd.routing.generate import declarations
    labels = {'available': '可用', 'dependency_ready': '依赖就绪才可用', 'experimental': '实验'}
    lines = ['# 本地文档格式能力', '', '由 `routing/declarations.json` 生成；`python -m openmd.engines.formats` 更新。', '',
        '分档描述实现与依赖合同，不保证任意文件都能完整提取。真实样本、工具版本和未验证项应保存在运行回执中。', '',
        '| 后缀 | 有序引擎 | 分档 | 范围与限制 |', '|---|---|---|---|']
    for item in declarations()['document_formats']:
        lines.append('| ' + '、'.join(item['extensions']) + ' | ' + ' → '.join(item['engines']) + ' | ' + labels[item['availability']] + ' | ' + item['scope'] + ' |')
    lines += ['', 'ZIP 是 Openmd 容器，不是 MarkItDown ZipConverter。ZIP 中媒体默认逐条返回 `asr_required`，图片返回 `ocr_required`；不会隐式上传或加载模型。', '',
        '附加引擎位于旧 PDF/Office/OCR provider 后；已有 PDF、DOCX、PPTX、XLSX 的正文和优先级保持原合同。', '']
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    path = Path(__file__).resolve().parents[3] / 'docs/subsystems/document-formats.generated.md'
    expected = render_formats()
    if args.check:
        return int(not path.is_file() or path.read_text(encoding='utf-8') != expected)
    path.write_text(expected, encoding='utf-8', newline='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
