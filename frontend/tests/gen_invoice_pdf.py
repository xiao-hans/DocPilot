"""生成 E2E 测试用发票 PDF（纯文本层，规避 OCR 回退）。

字段顺序必须满足后端 MockExtractor 的自左向右顺序扫描约定：
发票号码(20位) → 开票日期 → 销售方名称 → 购买方名称 → 价税合计 → 税额。
单页非空白字符远超 MIN_TEXT_CHARS_PER_PAGE=30，走 PDFTextParser 文本路径。
"""
import sys

import fitz

OUT = sys.argv[1] if len(sys.argv) > 1 else "invoice_240615.pdf"

lines = [
    "电子发票（增值税普通发票）",
    "发票号码：01100200011100045239",
    "开票日期：2024-06-15",
    "销售方名称：北京恒达科技有限公司",
    "购买方名称：上海锐进商贸有限公司",
    "价税合计（小写）：¥1,280.00",
    "税额：¥73.92",
    "收款人：王晓明",
    "备注：本发票内容由 DocAI 自动化测试生成",
]

doc = fitz.open()
page = doc.new_page(width=595, height=842)
y = 80
for line in lines:
    page.insert_text((72, y), line, fontsize=13, fontname="china-s")
    y += 26
doc.set_metadata({"title": "测试发票", "creator": "docai-e2e"})
doc.save(OUT)
doc.close()

# 自检：读回文本层，确认中文字符可提取
check = fitz.open(OUT)
text = check[0].get_text("text")
check.close()
non_space = sum(1 for c in text if not c.isspace())
print(f"PDF_WRITTEN {OUT} chars={non_space}")
if non_space < 60:
    sys.exit("SELF_CHECK FAIL: text layer too thin, OCR fallback would trigger")
if "01100200011100045239" not in text:
    sys.exit("SELF_CHECK FAIL: invoice number missing from extracted text")
print("SELF_CHECK OK")
