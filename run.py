#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""daily_arxiv 命令行入口。

读取本地 PDF 文献库, 从 arXiv 检索并推荐相关的新论文和经典论文, 附上 AI 生成的
内容讲解、与你已有工作的关联、以及可结合的研究方向。

要图形界面就运行 ``python ui.py``。

用法示例:
    python run.py                          # 默认推荐 20 篇
    python run.py --top 30                 # 推荐 30 篇
    python run.py --queries "determinant quantum Monte Carlo" "sign problem"
    python run.py --no-ai                  # 不调用 AI, 纯关键词排序
    python run.py --depth metadata         # 只读标题和摘要 (最快)
    python run.py --depth fulltext         # 连全文节选一起读
    python run.py --stage rank             # 只跑到排序, 不生成报告
    python run.py --refresh                # 清空 HTTP/AI 缓存重新抓取
    python run.py --refresh-pdf            # 忽略 PDF 索引, 重新解析所有 PDF
    python run.py --no-history             # 忽略推荐记录, 当全新的一轮跑

实现在 ``arxiv_rec/cli.py`` —— 图形界面入口 (``ui.py --run``) 和打包后的 exe
调的都是它, 免得同一套参数在两处各写一遍然后慢慢跑偏。
"""

from __future__ import annotations

import os
import sys

# 保证从任意目录运行都能 import 到包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arxiv_rec.cli import run

if __name__ == "__main__":
    sys.exit(run())
