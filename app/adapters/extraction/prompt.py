"""构建 LLM 提取提示词。"""

from __future__ import annotations

from app.models.schema import FieldDefinition

DOC_START = "=====DOCUMENT START====="
DOC_END = "=====DOCUMENT END====="


def build_extraction_prompt(
    fields: list[FieldDefinition], text: str, metadata: dict | None = None
) -> tuple[str, str]:
    """构建 (system_prompt, user_prompt)。

    - 字段清单（名称/类型/描述）
    - 强分隔符包裹的文档文本
    - 文件 metadata（文件名/页数）
    - 要求仅输出 JSON 对象
    """
    field_lines = [f"- {f.name}（类型：{f.type}）：{f.description}" for f in fields]
    fields_block = "\n".join(field_lines)

    meta_lines: list[str] = []
    if metadata:
        filename = metadata.get("filename")
        page_count = metadata.get("page_count") or metadata.get("pages")
        if filename:
            meta_lines.append(f"文件名：{filename}")
        if page_count is not None:
            meta_lines.append(f"页数：{page_count}")
    meta_block = "\n".join(meta_lines) if meta_lines else "（无）"

    system_prompt = (
        "你是一个严谨的文档信息提取助手。你的任务是根据用户给定的字段清单，"
        "从提供的文档文本中提取对应字段的值。\n"
        "规则：\n"
        "1. 仅输出一个 JSON 对象，不要包含任何解释、注释或 markdown 代码围栏。\n"
        "2. JSON 的键必须严格等于字段名称，顺序与字段清单一致。\n"
        "3. 数字类型输出 JSON number（不含千分位逗号、不含单位）；"
        "日期类型输出 ISO 字符串 YYYY-MM-DD。\n"
        "4. 文本中确实不存在的字段，值置为 null 或空字符串，不得编造。\n"
        "5. 只返回 JSON 对象本身。"
    )

    user_prompt = (
        f"# 待提取字段\n{fields_block}\n\n"
        f"# 文件信息\n{meta_block}\n\n"
        f"# 文档文本\n{DOC_START}\n{text}\n{DOC_END}\n\n"
        "请严格按照系统指令，仅输出 JSON 对象。"
    )
    return system_prompt, user_prompt
