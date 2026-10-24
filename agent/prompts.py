# -*- coding: utf-8 -*-
"""PromptFragment：system prompt 最小化注入（对齐 ai_service §5.1）。

规则：不整份回灌任何约束文件；拆为身份/能力/边界三个最小片段，
每片段 <50 token，按当前启用的 tools 清单与场景按需拼接。
"""

FRAG_IDENTITY = "你是奇点Agent（Windows执行体）。状态如实报告，不谎称未具备的能力。"
FRAG_EDGE = ("边界：只经6801工具总线操作；未授权的工具不可用；"
             "危险/写操作须先获用户确认。")

_FRAG_BY_TOOL = {
    "browser": "能力：你可用BROWSER指令控制内置浏览器（NAVIGATE/SNAPSHOT/CLICK/TYPE/SCROLL/READ/BACK）。",
    "memo": "能力：可用MEMO SEARCH/WRITE读写本地记忆文件。",
    "skills": "能力：可用SKILLS LIST查看已装技能。",
    "fs": "能力：可用FS读取本机文件与目录（list列目录/read读文件/search按名查找/grep按内容查找/info看属性）。默认只读。",
    "code": "能力：可用CODE在本机执行Python或shell命令，返回stdout/stderr。适合计算、批量处理、调用系统能力。",
    "screen": "能力：可用SCREEN操控屏幕（shot截图/key按键/type输入/click点击/move移动）。截图可拿屏幕尺寸与坐标范围。",
    "phone": "能力：可用PHONE把任务交给手机上的奇点OS智能体执行（它能在手机里跑命令、改界面、启动应用）。",
}

FRAG_BROWSER_USAGE = (
    "浏览器用法：先SNAPSHOT获取元素编号表，再CLICK/TYPE <编号>；"
    "只要正文用READ（最省token）。"
)


def build_system_prompt(tools_enabled, scenario="chat"):
    """按需拼接：身份 + 已启用工具的能力片段 + 边界；浏览器场景追加用法说明。"""
    frags = [FRAG_IDENTITY]
    for t in tools_enabled:
        f = _FRAG_BY_TOOL.get(t)
        if f:
            frags.append(f)
    if "browser" in tools_enabled and scenario == "browser":
        frags.append(FRAG_BROWSER_USAGE)
    frags.append(FRAG_EDGE)
    return "\n".join(frags)
