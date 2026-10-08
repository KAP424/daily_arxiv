"""arxiv_rec - 基于本地 PDF 文献库的 arXiv 相关文献推荐系统。

流程:
    本地 PDF 读取 (三档深度) -> 研究画像构建 -> arXiv 检索 (Atom API + RSS)
    -> 去重过滤 -> 相关性/时效性/重要性联合排序 -> AI 深度解读 -> Markdown 报告

文献来源只有本地 PDF 文件夹, 不读 Zotero 数据库。
"""

__version__ = "1.1.2"

__all__ = ["__version__"]
