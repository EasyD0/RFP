# -*- coding: utf-8 -*-
"""
生成《误报识别项目》汇报 PPT。
运行: D:\\Conda_Env\\codeA\\python.exe make_ppt.py
依赖: python-pptx
可复用: 补充结果数据后重跑即可更新 PPT（结果页用标记 TODO_METRICS 标注待填）。
"""
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# ---------- 配色 ----------
NAVY   = RGBColor(0x1F, 0x38, 0x64)   # 深蓝 主色
BLUE   = RGBColor(0x2E, 0x75, 0xB6)   # 亮蓝 强调
LIGHT  = RGBColor(0xEA, 0xF1, 0xF8)   # 浅蓝 底
DARK   = RGBColor(0x1E, 0x29, 0x3B)   # 正文深色
GRAY   = RGBColor(0x64, 0x74, 0x8B)   # 次要灰
WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
RED    = RGBColor(0xC0, 0x39, 0x2B)   # 待填标记色
LINEB  = RGBColor(0xBF, 0xD3, 0xEA)   # 表格线/分隔

FONT = "微软雅黑"
SW, SH = Inches(13.333), Inches(7.5)

prs = Presentation()
prs.slide_width, prs.slide_height = SW, SH
BLANK = prs.slide_layouts[6]

# ---------- 工具 ----------
def _rect(slide, x, y, w, h, color):
    sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    sh.fill.solid(); sh.fill.fore_color.rgb = color
    sh.line.fill.background()
    sh.shadow.inherit = False
    return sh

def _txt(slide, x, y, w, h, text, size=18, color=DARK, bold=False,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, wrap=True):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame; tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Pt(4)
    tf.margin_top = tf.margin_bottom = Pt(2)
    p = tf.paragraphs[0]; p.alignment = align
    r = p.add_run(); r.text = text
    f = r.font; f.name = FONT; f.size = Pt(size)
    f.bold = bold; f.color.rgb = color
    return box

def _mul(slide, x, y, w, h, lines, size=18, color=DARK, space=6, anchor=MSO_ANCHOR.TOP, bullet=True):
    """lines: list of (text, bold) 或 str"""
    text_box = slide.shapes.add_textbox(x, y, w, h)
    tf = text_box.text_frame; tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Pt(4)
    tf.margin_top = tf.margin_bottom = Pt(2)
    first = True
    for item in lines:
        if isinstance(item, str):
            t, bold = item, False
        else:
            t, bold = item
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(space)
        if bullet:
            p.text = "•  " + t
        else:
            p.text = t
        for run in p.runs:
            run.font.name = FONT; run.font.size = Pt(size); run.font.color.rgb = color
            run.font.bold = bold
    return text_box

def _header(slide, title, section=""):
    """顶部标题栏 + 底部页码"""
    _rect(slide, 0, 0, SW, Inches(0.9), NAVY)
    leftbar = _rect(slide, Inches(0.35), Inches(0.24), Inches(0.12), Inches(0.42), BLUE)
    _txt(slide, Inches(0.62), Inches(0.16), Inches(10.5), Inches(0.6),
         title, size=26, color=WHITE, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    if section:
        _txt(slide, Inches(11.2), Inches(0.28), Inches(1.9), Inches(0.4),
             section, size=12, color=RGBColor(0xBF, 0xD3, 0xEA),
             align=PP_ALIGN.RIGHT)

def _footer(slide, num):
    _rect(slide, 0, SH - Inches(0.3), SW, Inches(0.3), WHITE)
    _txt(slide, 0, SH - Inches(0.34), SW, Inches(0.3),
         f"{num}", size=11, color=GRAY, align=PP_ALIGN.RIGHT)

def _band(slide, num, title, section=""):
    _header(slide, title, section)
    _footer(slide, num)

def _png_size(path):
    """从 PNG 头(第16字节起 IHDR 的宽高)读取像素尺寸，避免依赖 Pillow。"""
    with open(path, "rb") as f:
        head = f.read(24)
    w = int.from_bytes(head[16:20], "big")
    h = int.from_bytes(head[20:24], "big")
    return w, h

def _image(slide, path, x, y, w, caption=None):
    """插图：按宽度 w 等比放置 PNG。"""
    iw, ih = _png_size(path)
    h = w / iw * ih
    box = slide.shapes.add_picture(path, x, y, width=Inches(w), height=Inches(h))
    if caption:
        _txt(slide, x, y + h + Inches(0.05), Inches(w), Inches(0.3),
             caption, size=11, color=GRAY, align=PP_ALIGN.CENTER)
    return box

def _table(slide, rows, x, y, widths, header=None, col_align=None,
           font_size=13, row_h=0.42, header_h=0.5):
    """rows: 二维文本列表; header: 可选表头; col_align: 每列对齐列表"""
    nrows = len(rows) + (1 if header else 0)
    ncols = len(rows[0])
    gt = slide.shapes.add_table(nrows, ncols, x, y,
                                Inches(sum(widths)), Inches(header_h + row_h * len(rows)))
    tbl = gt.table
    # 关闭默认样式以自绘
    tbl.first_row = True; tbl.horz_banding = False
    for j, w in enumerate(widths):
        tbl.columns[j].width = Inches(w)
    tbl.rows[0].height = Inches(header_h)
    for i in range(1, nrows):
        tbl.rows[i].height = Inches(row_h)
    aligns = col_align or [PP_ALIGN.LEFT] * ncols

    def fill_cell(cell, text, bg, fg, bold, align, size):
        cell.fill.solid(); cell.fill.fore_color.rgb = bg
        cell.margin_left = cell.margin_right = Pt(6)
        cell.margin_top = cell.margin_bottom = Pt(2)
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf = cell.text_frame; tf.word_wrap = True
        p = tf.paragraphs[0]; p.alignment = align
        r = p.add_run(); r.text = text
        r.font.name = FONT; r.font.size = Pt(size); r.font.bold = bold; r.font.color.rgb = fg

    for j in range(ncols):
        if header:
            fill_cell(tbl.cell(0, j), header[j], NAVY, WHITE, True, aligns[j], font_size)
    for i, row in enumerate(rows, start=(1 if header else 0)):
        bg = WHITE if (i % 2 == 1) else LIGHT
        for j, val in enumerate(row):
            fill_cell(tbl.cell(i, j), val, bg, DARK, False, aligns[j], font_size)
    return tbl

# ============================================================
# 1. 封面
# ============================================================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, SW, SH, NAVY)
_rect(s, 0, Inches(4.9), SW, Inches(0.06), BLUE)
_txt(s, Inches(0.9), Inches(1.9), Inches(11.5), Inches(1.1),
     "静态分析误报自动识别", size=48, color=WHITE, bold=True)
_txt(s, Inches(0.9), Inches(3.05), Inches(11.5), Inches(0.7),
     "基于 clang 的无虚警误报判定  (ReduceFalsePositives)", size=24, color=RGBColor(0xBF, 0xD3, 0xEA))
_txt(s, Inches(0.9), Inches(5.2), Inches(11.5), Inches(0.5),
     "汇报人 / 部门 / 日期", size=16, color=RGBColor(0x8C, 0xA8, 0xC9))

# ============================================================
# 2. 目录
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 2, "目录", "CONTENTS")
toc = [
    ("1", "背景与目标", "静态分析误报多，人工复核成本高"),
    ("2", "为什么难", "误报判定的不可判定性"),
    ("3", "核心解法", "无虚警保证：宁可漏判，绝不误杀"),
    ("4", "技术选型", "正则 / libclang / clangd 三板斧"),
    ("5", "规则覆盖", "7 条规则 × 判定依据"),
    ("6", "工程架构", "分层 + 多进程 + compile_commands"),
    ("7", "落地效果", "误报率 / 效率 / 成本"),
    ("8", "挑战与下一步", "保守边界与扩展方向"),
]
y = Inches(1.25)
for num, t, d in toc:
    _txt(s, Inches(0.9), y, Inches(0.7), Inches(0.5), num, size=26, color=BLUE, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    _txt(s, Inches(1.7), y, Inches(4.0), Inches(0.5), t, size=20, color=DARK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    _txt(s, Inches(5.5), y, Inches(7.0), Inches(0.45), d, size=15, color=GRAY, anchor=MSO_ANCHOR.MIDDLE)
    y += Inches(0.68)

# ============================================================
# 3. 背景与目标
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 3, "1 · 背景与目标", "痛点")
left = [
    "静态分析工具会在源码上报告大量“问题”，每条含文件名、函数、代码行、规则名等。",
    "其中相当一部分是误报 —— 代码实际没有违反规则，分析器却报了。",
    "现状：需要人工一条条点开、看代码、判断真伪。",
    "成本高、枯燥、判据因人而异，是测试流水线的典型瓶颈。",
]
_mul(s, Inches(0.9), Inches(1.4), Inches(7.2), Inches(4.5), left, size=18, space=14)
# 右侧漏斗示意
_bx = Inches(8.7); _bw = Inches(3.9)
_rect(s, _bx, Inches(1.5), _bw, Inches(0.7), BLUE)
_txt(s, _bx, Inches(1.52), _bw, Inches(0.66), "上报问题总数 (n)", size=15, color=WHITE, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
_rect(s, Inches(8.95), Inches(2.35), _bw - Inches(0.5), Inches(0.7), RGBColor(0x7F, 0xA8, 0xD0))
_txt(s, Inches(8.95), Inches(2.42), _bw - Inches(0.5), Inches(0.56), "真实问题 (少数)", size=14, color=WHITE, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
_rect(s, Inches(9.2), Inches(3.2), _bw - Inches(1.0), Inches(0.7), RGBColor(0xB0, 0xC9, 0xE6))
_txt(s, Inches(9.2), Inches(3.27), _bw - Inches(1.0), Inches(0.56), "误报 (多数)", size=14, color=NAVY, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
_txt(s, Inches(8.7), Inches(4.15), _bw, Inches(0.6), "误报比例越高，人工复核浪费越严重", size=14, color=GRAY, align=PP_ALIGN.CENTER)
_footer(s, 3)

# ============================================================
# 3B. 误报识别价值管道（数据流图）
# ============================================================
B = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagrams", "05_误报识别管道", "误报识别管道.png")
if os.path.exists(B):
    s = prs.slides.add_slide(BLANK)
    _band(s, 0, "图解 · 误报识别价值管道", "DATA FLOW")
    _image(s, B, Inches(1.92), Inches(1.35), Inches(9.5),
           "报告 → 自动判定 → 误报打 tag / 真实问题 → 仅人工复核真实问题")

# ============================================================
# 4. 为什么难
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 4, "2 · 为什么这件事“难”", "挑战")
bx = Inches(0.9)
_txt(s, bx, Inches(1.3), Inches(11.5), Inches(0.5),
     "“某个问题是不是误报”本质上是一个语义判断，而这类判断往往不可判定：", size=19, color=DARK, bold=True)
_rect(s, bx, Inches(2.0), Inches(11.5), Inches(2.5), LIGHT)
_mul(s, Inches(1.15), Inches(2.15), Inches(11.0), Inches(2.3), [
    "✔ 例子：判断“数组会不会越界” —— 对任意 C 程序不存在通用的精确算法（可归约到停机问题 / Rice 定理，证明见附录）。",
    "✔ 这解释了两件事：① 为什么分析器自身会误报（它必须在漏报与误报之间取舍）；② 为什么自动判误报容易掉进“启发式猜法”的坑。",
], size=17, space=12)
_mul(s, bx, Inches(4.85), Inches(11.5), Inches(2.0), [
    ("启发式猜法（按函数名、看着像就判）的代价：", True),
    "用“把真缺陷也放走”去换“识别得多”，是拿正确性冒险 —— 这是本项目明确拒绝的。",
], size=17, space=8)
_footer(s, 4)

# ============================================================
# 5. 核心解法：无虚警保证
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 5, "3 · 核心解法：无虚警保证", "灵魂")
_txt(s, Inches(0.9), Inches(1.45), Inches(11.5), Inches(0.7),
     "只要被识别为误报，就必须（高概率确定）是误报；不确定时，宁可保守不判。", size=20, color=NAVY, bold=True)
_txt(s, Inches(0.9), Inches(2.15), Inches(11.5), Inches(0.6),
     "→ 宁可漏判，绝不误杀。", size=24, color=BLUE, bold=True)
_rect(s, Inches(0.9), Inches(3.15), Inches(11.5), Inches(0.05), LINEB)
_txt(s, Inches(0.9), Inches(3.35), Inches(11.5), Inches(0.5),
     "判了必对的“三类可验证依据”", size=17, color=DARK, bold=True)
_table(s, [
    ["形式固定的写法（正则精确匹配）", "宏调用、 (void) 表达式、 &var、 #pragma inline_asm"],
    ["可判定的语法 / 语义属性", "全局作用域、静态存储期、编译期常量下标"],
    ["可靠的 AST 语义信息（libclang）", "存储类别、链接属性、canonical 类型、数组长度"],
], Inches(0.9), Inches(3.95), [3.8, 7.7])
_footer(s, 5)

# ============================================================
# 5B. 误报判定流程（workflow 图）
# ============================================================
W = os.path.join(os.path.dirname(os.path.abspath(__file__)), "误报判定流程.png")
if os.path.exists(W):
    s = prs.slides.add_slide(BLANK)
    _band(s, 0, "图解 · 单条问题的误报判定流程", "WORKFLOW")
    _image(s, W, Inches(2.42), Inches(1.3), Inches(8.5),
           "命中误报即短路并打 tag；无法证明的一律保守保留 → 宁缺毋滥、判了必对")

# ============================================================
# 6. 技术三板斧
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 6, "4 · 技术选型：三板斧", "技术")
_table(s, [
    ["正则 / 文本匹配", "识别写法固定的模式", "宏调用、(void)、&var 等；成本最低，形式固定时正确性可保证", "成本最低"],
    ["libclang（AST）", "语法 + 语义分析", "存储类别、类型、数组长度、枚举值、宏定义值等可靠信息", "语义可靠"],
    ["clangd（LSP）", "跨翻译单元的符号引用", "查“所有引用处”（如返回值是否被使用）", "引用最完整"],
], Inches(0.9), Inches(1.6), [1.9, 3.6, 5.4, 1.6])
_rect(s, Inches(0.9), Inches(4.6), Inches(11.5), Inches(1.7), LIGHT)
_mul(s, Inches(1.15), Inches(4.75), Inches(11.0), Inches(1.5), [
    ("亮点：clangd 用长驻会话 + compile_commands.json 实现跨编译单元的引用追溯", True),
    "常用方案答不出“这个返回值在全部调用点都没人用”，我们能做到 —— 这是 36S 规则的关键支撑。",
], size=16, space=8)
_footer(s, 6)

# ============================================================
# 6B. clangd 跨翻译单元引用查询（sequence 图）
# ============================================================
SQ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clangd引用查询.png")
if os.path.exists(SQ):
    s = prs.slides.add_slide(BLANK)
    _band(s, 0, "图解 · 36S：clangd 跨翻译单元引用查询", "SEQUENCE")
    _image(s, SQ, Inches(3.17), Inches(1.3), Inches(7.0),
           "rule → clangd_tool → clangd(references) → libclang AST 验证返回值是否被使用")

# ============================================================
# 7. 规则覆盖
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 7, "5 · 规则覆盖", "7 条规则")
rows = [
    ["69D", "变量未赋值就使用", "声明处初始化 / 全局与静态零初始化 / 中途初始化 / 取地址"],
    ["57S", "无作用的语句", "(void)·宏占位·日志·汇编·声明·函数调用"],
    ["1X", "声明类型不一致", "两处声明 storage/linkage/canonical type 逐一比对"],
    ["47S", "数组越界", "字面量 / 枚举 / 宏 / 变量下标边界约束分析"],
    ["36S", "函数没有返回语句", "“所有引用处都不使用返回值” 判定"],
    ["132S", "逻辑表达式中使用赋值", "赋值结果以“取值”方式被消费 → 判误报"],
    ["404S", "字符串数组越界初始化", "初始化列表各维度数量不超声明大小（纯文本 + libclang）"],
]
_table(s, rows, Inches(0.9), Inches(1.45), [0.9, 3.4, 7.7],
       col_align=[PP_ALIGN.CENTER, PP_ALIGN.LEFT, PP_ALIGN.LEFT], font_size=13, row_h=0.55)
_txt(s, Inches(0.9), Inches(6.5), Inches(11.5), Inches(0.5),
     "每项判定输出带可追溯 tag（如 <已在声明处显式初始化>），支持人工抽查复核。", size=14, color=GRAY)
_footer(s, 7)

# ============================================================
# 8. 工程架构
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 8, "6 · 工程架构", "工程")
# 数据流条
flow = [("报告入口", "data_structure"), ("编译上下文", "context + Preprocessor"), ("调度", "runner"), ("各规则", "rules/")]
fx = Inches(0.9); fw = Inches(2.7); gap = Inches(0.18)
for i, (name, mod) in enumerate(flow):
    x = fx + i * (fw + gap)
    _rect(s, x, Inches(1.55), fw, Inches(0.85), NAVY)
    _txt(s, x, Inches(1.6), fw, Inches(0.38), name, size=17, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
    _txt(s, x, Inches(2.0), fw, Inches(0.34), mod, size=12, color=RGBColor(0xBF, 0xD3, 0xEA), align=PP_ALIGN.CENTER)
    if i < len(flow) - 1:
        _txt(s, x + fw - Inches(0.06), Inches(1.78), Inches(0.4), Inches(0.4), "→", size=20, color=BLUE, bold=True)
_mul(s, Inches(0.9), Inches(3.0), Inches(11.5), Inches(3.6), [
    ("多进程并行：worker 复用反序列化后的编译上下文，避免重复初始化；退出时清理孤儿 clangd 进程", True),
    ("compile_commands.json：给 clangd / libclang 统一提供编译参数，保证解析正确", True),
    ("模块化注册机制：新规则“导入即注册”，扩展成本低（易对接“多跑几条规则”）", True),
    ("外层“建议式”：识别为误报的条目不删除，仅打提示标签 → 天然可回退、可审计", True),
], size=16, space=12)
_footer(s, 8)

# ============================================================
# 8B. 工程架构全视图（architecture 图）
# ============================================================
A = os.path.join(os.path.dirname(os.path.abspath(__file__)), "架构图.png")
if os.path.exists(A):
    s = prs.slides.add_slide(BLANK)
    _band(s, 0, "图解 · 检查框架架构全视图", "ARCHITECTURE")
    _image(s, A, Inches(1.92), Inches(1.35), Inches(9.5),
           "内部判定框架 + 语义后端（libclang / clangd）与编译数据库的依赖关系")

# ============================================================
# 8C. Problem 判定生命周期（lifecycle 图 · 建议式）
# ============================================================
L = os.path.join(os.path.dirname(os.path.abspath(__file__)), "判定生命周期.png")
if os.path.exists(L):
    s = prs.slides.add_slide(BLANK)
    _band(s, 0, "图解 · 问题判定生命周期（建议式）", "LIFECYCLE")
    _image(s, L, Inches(2.67), Inches(1.3), Inches(8.0),
           "无论判误报与否，条目都不删除：命中打 tag，未命中保留待人工复核")

# ============================================================
# 9. 落地效果
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 9, "7 · 落地效果", "结果")
M = "【待填】"
cards = [
    ("误报率", "识别误报数 / 上报问题总数", M),
    ("节约人工", "节省复核工时 / 条数", M),
    ("处理速度", "条 / 分钟（含多进程提升倍数）", M),
]
cw = Inches(3.6); cgap = Inches(0.33); cx = Inches(0.9)
for i, (title, sub, val) in enumerate(cards):
    x = cx + i * (cw + cgap)
    _rect(s, x, Inches(1.55), cw, Inches(1.55), LIGHT)
    _txt(s, x, Inches(1.68), cw, Inches(0.5), title, size=18, color=NAVY, bold=True, align=PP_ALIGN.CENTER)
    _txt(s, x, Inches(2.15), cw, Inches(0.45), val, size=30, color=RED, bold=True, align=PP_ALIGN.CENTER)
    _txt(s, x, Inches(2.7), cw, Inches(0.34), sub, size=12, color=GRAY, align=PP_ALIGN.CENTER)

_rect(s, Inches(0.9), Inches(3.5), Inches(11.2), Inches(2.5), WHITE)
_txt(s, Inches(1.15), Inches(3.7), Inches(10.7), Inches(0.5),
     "规则覆盖：已实现 7 条，命中即“可判定”", size=18, color=NAVY, bold=True)
_mul(s, Inches(1.15), Inches(4.3), Inches(10.7), Inches(1.6), [
    "所有判定输出均带可追溯 tag，支持抽查复核（可审计）。",
    ("副本地由 5 条扩展至 7 条（新增 132S / 404S），持续演进中。", True),
], size=16, space=8)
_txt(s, Inches(0.9), Inches(6.55), Inches(11.5), Inches(0.4),
     "结果数据将在实跑补全后填入（标记为红色【待填】）。", size=13, color=GRAY)
_footer(s, 9)

# ============================================================
# 10. 挑战与下一步
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 10, "8 · 挑战与下一步", "展望")
_rect(s, Inches(0.9), Inches(1.45), Inches(11.2), Inches(2.2), LIGHT)
_txt(s, Inches(1.15), Inches(1.6), Inches(10.7), Inches(0.4), "现在刻意保守，覆盖还不广", size=17, color=NAVY, bold=True)
_mul(s, Inches(1.15), Inches(2.05), Inches(10.7), Inches(1.5), [
    "“中途初始化”在分支路径 / goto 控制流下的完整数据流追踪尚未完备（宁缺毋滥）。",
    "复杂下标、指针别名等场景未追踪，同样保守不判。",
], size=15, space=8)
_txt(s, Inches(0.9), Inches(4.1), Inches(11.5), Inches(0.4), "下一步", size=17, color=NAVY, bold=True)
_mul(s, Inches(0.9), Inches(4.55), Inches(11.5), Inches(2.2), [
    "放开覆盖率边界：更多下标的可行判定域、完整初始化路径追踪。",
    "扩展更多规则（已从 5 → 7）。",
    "接入现有构建 / 测试流水线，做常态化跑批并沉淀历史误报标签。",
], size=16, space=10)
_footer(s, 10)

# ============================================================
# 11. 总结
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 11, "总结", "SUMMARY")
concl = [
    ("以“无虚警保证”为原则，", "正面回答案了“自动判误报”这个不可判定难题。"),
    ("用 libclang + clangd 的组合，", "把判断建立在可验证的语义上，而非猜测上。"),
    ("覆盖 7 条规则，", "把人从大量无效人工复核里解放出来。"),
]
y = Inches(1.7)
for h, r in concl:
    _txt(s, Inches(1.1), y, Inches(3.4), Inches(0.6), h, size=20, color=BLUE, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    _txt(s, Inches(4.5), y, Inches(8.0), Inches(0.6), r, size=19, color=DARK, anchor=MSO_ANCHOR.MIDDLE)
    y += Inches(1.05)
_txt(s, Inches(0.9), Inches(5.6), Inches(11.5), Inches(0.8),
     "—— 欢迎提问 ——", size=20, color=GRAY, align=PP_ALIGN.CENTER)
_footer(s, 11)

# ============================================================
# 12. 附录 A：不可判定性证明
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 12, "附录 A · 为什么“数组越界”不可判定", "附录")
_mul(s, Inches(0.9), Inches(1.35), Inches(11.5), Inches(2.6), [
    "把“运行中是否发生越界”归约到停机问题（Rice 定理 + 非平凡语义性质）。",
    "结论：不存在对任意程序精确判定越界的通用算法。",
    "由此推出两条：① 分析器自身只能近似，这正是误报的来源；② 我们也只能做“保守近似”，于是“无虚警保证”不只是一句口号，而是理论约束下的必然策略。",
], size=17, space=12)
_rect(s, Inches(0.9), Inches(4.25), Inches(11.5), Inches(2.3), LIGHT)
_txt(s, Inches(1.15), Inches(4.4), Inches(11.0), Inches(0.5),
     "归约构造（示意）", size=16, color=NAVY, bold=True)
_mul(s, Inches(1.15), Inches(4.9), Inches(11.0), Inches(1.6), [
    "int main(){ int arr[1]; simulate(M, w);   // 仅当 M(w) 停机时返回",
    "    arr[1];                              // 越界访问",
    "    return 0; }                           // M(w) 停机 ⇔ 发生越界",
], size=14, space=4)
_footer(s, 12)

# ============================================================
# 13. 附录 B：讲解话术
# ============================================================
s = prs.slides.add_slide(BLANK)
_band(s, 13, "附录 B · 讲解话术", "附录")
_mul(s, Inches(0.9), Inches(1.5), Inches(11.5), Inches(3.0), [
    "谈保守策略：“我们认错的一定是错的；宁可多留几条让分析器接着报，也不放走一个真 bug。”",
    "谈 clangd 差异：“别的做法答不出‘这个返回值在所有调用点都没人用’，我们能在编译单元维度追到所有引用。”",
    "谈成本取舍：“盲目追求全自动 = 拿正确性赌；我们选先保对、再扩量。”",
], size=17, space=18)
_txt(s, Inches(0.9), Inches(5.4), Inches(11.5), Inches(0.6),
     "以上与报告文档《说明.md》一致，供答辩 / 评审随时展开理论细节。", size=14, color=GRAY)
_footer(s, 13)

# ============================================================
# 14. 致谢
# ============================================================
s = prs.slides.add_slide(BLANK)
_rect(s, 0, 0, SW, SH, NAVY)
_rect(s, 0, Inches(4.4), SW, Inches(0.06), BLUE)
_txt(s, Inches(0.9), Inches(2.6), Inches(11.5), Inches(1.0), "感谢聆听", size=46, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
_txt(s, Inches(0.9), Inches(4.7), Inches(11.5), Inches(0.5),
     "任一“问题”是否误报，若有疑问欢迎复核各判定 tag", size=16, color=RGBColor(0xBF, 0xD3, 0xEA), align=PP_ALIGN.CENTER)

# ---------- 保存 ----------
# 页脚页码自动重排（按最终页序）
for idx, sl in enumerate(prs.slides, start=1):
    for sh in sl.shapes:
        if getattr(sh, "has_text_frame", False):
            t = sh.text_frame.text.strip()
            if t and t.isdigit():
                sh.text_frame.text = str(idx)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "误报识别项目_汇报.pptx")
prs.save(out)
print("已生成:", out, "共", len(prs.slides), "页")