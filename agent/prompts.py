# -*- coding: utf-8 -*-
"""PromptFragment：system prompt 最小化注入（对齐 ai_service §5.1）。

规则：不整份回灌任何约束文件；拆为身份/能力/边界三个最小片段，
每片段 <50 token，按当前启用的 tools 清单与场景按需拼接。
"""
import os

FRAG_IDENTITY = "你是intent（Windows执行体）。状态如实报告，不谎称未具备的能力。"
FRAG_EDGE = ("边界：只经6801工具总线操作；未授权的工具不可用；"
             "危险/写操作须先获用户确认。")
# 经济风格（对齐 pi 的 "Be concise"）：显著减少输出 token 与来回轮数
FRAG_STYLE = ("风格：先结论后细节，简洁作答；不复述工具原始输出；"
              "不要说\"将要\"做什么，直接调用工具；能一次问清就不要反复确认。")

_FRAG_BY_TOOL = {
    "browser": "能力：你可用BROWSER指令控制内置浏览器（NAVIGATE/SNAPSHOT/CLICK/TYPE/SCROLL/READ/BACK）。",
    "memo": ("能力：可用MEMO读写长期记忆（跨会话持久化）。op=write 写入（自动去重合并），"
             "op=search 检索，op=list 列出，op=delete 删除。kind=profile档案/preference偏好/"
             "fact事实/task待办。用户说「记住…」「我喜欢…」「我叫…」时应主动写入。"),
    "skills": "能力：可用SKILLS LIST查看已装技能。",
    "fs": "能力：可用FS读取本机文件与目录（list列目录/read读文件/search按名查找/grep按内容查找/info看属性）。大文件用 offset/limit 分段读，不要整文件读取。默认只读。",
    "code": "能力：可用CODE在本机执行Python或shell命令，返回stdout/stderr。适合计算、批量处理、调用系统能力。",
    "screen": "能力：可用SCREEN操控屏幕（shot截图/key按键/type输入/click点击/move移动）。截图可拿屏幕尺寸与坐标范围。",
    "phone": "能力：可用PHONE把任务交给手机上的奇点OS智能体执行（它能在手机里跑命令、改界面、启动应用）。",
    "kb": ("能力：可用KB检索知识库。op=search 按文件名查路径（秒查）；"
           "op=grep 检索已入库文档的正文内容（回答「文件里写了什么」）；"
           "op=ingest 把文档正文入库以便检索；op=stats 看索引状态。"),
    "ctx": "能力：可用CTX查看与调试自身上下文（inspect 查看容量/占用/事实摘要/消息条数；facts_add 增事实；facts_remove 删事实；reset 清空事实；compact 压缩历史；limit 看/设容量 tok）。",
    "web": "能力：可用WEB联网搜索（op=search 查资料）与抓取网页正文（op=fetch 取正文文本）。适合查最新信息、读网页内容。",
    "organize": "能力：可用ORGANIZE整理目录（op=plan 预览/op=run 执行，按扩展名分组）与读取文档正文（op=doc，支持 pdf/docx/xlsx/txt）。",
    "remind": "能力：可用REMIND设提醒（op=add 设一个未来时刻的提醒并到点弹出；op=list 查看；op=clear 清空）。",
}

FRAG_BROWSER_USAGE = (
    "浏览器用法：先SNAPSHOT获取元素编号表，再CLICK/TYPE <编号>；"
    "只要正文用READ（最省token）。"
)


def _skills_digest(skills_dir):
    """扫描 skills/*.qdskill，生成「绝对路径（desc）」摘要，供 system prompt 注入。

    必须给出 .qdskill 绝对路径：模型据此用 FS 读取原文里的启动命令；
    否则遇到"打开工作台"这类指令会盲猜（例如误操作同名桌面窗口）。
    """
    if not skills_dir or not os.path.isdir(skills_dir):
        return ""
    items = []
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".qdskill"):
            continue
        path = os.path.join(skills_dir, fn)
        desc = ""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("desc:"):
                        desc = s[5:].strip()
                        break
        except Exception:
            continue
        items.append("%s（%s）" % (path, desc) if desc else path)
    return "；".join(items)


def build_system_prompt(tools_enabled, scenario="chat", skills_dir=None):
    """按需拼接：身份 + 已启用工具的能力片段 + 边界；浏览器场景追加用法说明。"""
    frags = [FRAG_IDENTITY]
    for t in tools_enabled:
        f = _FRAG_BY_TOOL.get(t)
        if f:
            frags.append(f)
    # 已装技能摘要：给出 .qdskill 绝对路径与用途，模型须先读原文再执行
    if "skills" in tools_enabled:
        digest = _skills_digest(skills_dir)
        if digest:
            frags.append("已装技能（命中时必须先用FS读取下列 .qdskill 原文，"
                         "严格按其中给出的路径与命令执行，不要自行搜索本机）：" + digest)
    if "browser" in tools_enabled and scenario == "browser":
        frags.append(FRAG_BROWSER_USAGE)
    frags.append(FRAG_STYLE)
    frags.append(FRAG_EDGE)
    return "\n".join(frags)