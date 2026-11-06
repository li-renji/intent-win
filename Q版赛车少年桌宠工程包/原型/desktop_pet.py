# -*- coding: utf-8 -*-
"""赛车少年桌宠 · 交互原型（真透明桌面窗口）

用现有素材做的可跑版本，验证「真透明 + 角色常驻 + 核心交互」方向是否正确。

运行：
    C:/Python314/python.exe desktop_pet.py
或双击 启动桌宠.bat

注：脚本用 ASCII 文件名，避免 cmd 在中文代码页下传参乱码。

技术要点：
  - tkinter + -transparentcolor 把品红 #FF00FF 抠成透明，窗口自身绘制，
    不经过 WebView2 的 DirectComposition（那条路在本机抠不动，已实测排除）。
  - 所有 RGBA 素材先「预合成到品红底」再交给 PhotoImage，避免 Tk 的
    alpha 合成把角色白色染黑/染紫（已实测）。
  - 透明区鼠标天然穿透到桌面，角色像素可点可拖。
"""
import os
import random
import sys
import threading
import time
import tkinter as tk
from PIL import Image, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
KEY = "#FF00FF"
KEY_RGB = (255, 0, 255)
W = H = 240
FPS_MS = 83          # 12fps
STATE_FILE = os.path.join(HERE, "pet_pos.json")

ACTIONS = ("wave", "jump", "think", "sit")
LINES = {
    "wave": ["嗨！我在～", "怎么啦？", "要不要聊两句"],
    "jump": ["哇！吓我一跳", "来劲了！", "再点一下试试"],
    "think": ["让我想想…", "嗯……", "好像有点道理"],
    "sit": ["坐一会儿", "休息一下下", "有点累了"],
    "drag": ["挪好啦～", "这个位置不错"],
    "talk": ["等新素材做好，我就能有更多动作啦！"],
}
CACHE = {}


def flat(im):
    """RGBA -> 预合成到品红底的 RGB（alpha 二值，边缘不出品红毛边）。"""
    im = im.convert("RGBA")
    a = im.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    out = Image.new("RGB", im.size, KEY_RGB)
    out.paste(im.convert("RGB"), (0, 0), a)
    return out


def load_frames(name):
    """懒加载某动作的帧（预合成后的 PIL 图），复用缓存。"""
    if name in CACHE:
        return CACHE[name]
    d = os.path.join(ASSETS, "frames", name)
    files = sorted(f for f in os.listdir(d) if f.endswith(".png")) if os.path.isdir(d) else []
    frames = [flat(Image.open(os.path.join(d, f))) for f in files]
    CACHE[name] = frames
    return frames


class Pet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY)
        self.root.config(bg=KEY)
        self.root.geometry(f"{W}x{H}+{self._load_pos()[0]}+{self._load_pos()[1]}")

        self.label = tk.Label(self.root, bg=KEY, bd=0)
        self.label.place(x=0, y=0, width=W, height=H)

        # 气泡：独立透明置顶窗口，跟随主窗口
        self.bub = tk.Toplevel(self.root)
        self.bub.overrideredirect(True)
        self.bub.attributes("-topmost", True)
        self.bub.attributes("-transparentcolor", KEY)
        self.bub.config(bg=KEY)
        self.bub.withdraw()
        self.bub_lbl = tk.Label(self.bub, bg="#FFFFFF", fg="#20263A",
                                font=("Microsoft YaHei", 10), wraplength=210,
                                padx=12, pady=8, justify="center")
        self.bub_lbl.pack()

        # 右键菜单
        self.menu = tk.Menu(self.root, tearoff=0, bg="#1B2133", fg="#E9EEFB",
                            activebackground="#2C3550", bd=0)
        for label, act in [("挥个手", "wave"), ("跳一下", "jump"),
                           ("思考一下", "think"), ("坐一会儿", "sit")]:
            self.menu.add_command(label=label, command=lambda a=act: self.play(a))
        self.menu.add_separator()
        self.menu.add_command(label="说句话", command=lambda: self.say(random.choice(LINES["talk"]), 3200))
        self.menu.add_command(label="回到原位", command=self.reset_pos)
        self.menu.add_separator()
        self.menu.add_command(label="让阿觉休息", command=self.quit)

        self.idle_img = flat(Image.open(os.path.join(ASSETS, "idle.png")))
        self.set_img(self.idle_img)

        # 状态
        self.state = "idle"
        self.busy = False
        self.frame_i = 0
        self.action_frames = []
        self.drag = None
        self.click_job = None
        self.bubble_job = None

        # 事件
        self.label.bind("<ButtonPress-1>", self.on_down)
        self.label.bind("<B1-Motion>", self.on_move)
        self.label.bind("<ButtonRelease-1>", self.on_up)
        self.label.bind("<Double-Button-1>", self.on_double)
        self.label.bind("<Button-3>", self.on_right)

        # 静态待机完全静止：不做呼吸位移，避免「作为图片时上下闪动」。
        self.schedule_idle()
        self.root.after(700, lambda: self.say("嗨！我是赛车少年～", 2600))

    # ------------------------------------------------------------ 位置
    def _load_pos(self):
        # 自动化验收用：PET_POS=x,y 指定独立位置，避免与已运行的实例重叠。
        override = os.environ.get("PET_POS")
        if override:
            try:
                sx, sy = override.split(",", 1)
                return int(sx), int(sy)
            except ValueError:
                pass
        try:
            import json
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return int(d["x"]), int(d["y"])
        except Exception:
            sw = self.root.winfo_screenwidth() if hasattr(self, "root") else 1920
            return sw - 320, 420

    def save_pos(self):
        try:
            import json
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
        except Exception:
            pass

    def reset_pos(self):
        self.root.geometry(f"{W}x{H}+{600}+{300}")
        self.save_pos()
        self.say("回来啦～", 1200)

    # ------------------------------------------------------------ 显示
    def set_img(self, img):
        self._photo = ImageTk.PhotoImage(img)
        self.label.config(image=self._photo)

    def say(self, text, ms=2200):
        self.bub_lbl.config(text=text)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.bub.geometry(f"+{x - 30}+{max(0, y - 76)}")
        self.bub.deiconify()
        if self.bubble_job:
            self.root.after_cancel(self.bubble_job)
        self.bubble_job = self.root.after(ms, self.bub.withdraw)

    def follow_bubble(self):
        if self.bub.state() == "normal":
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self.bub.geometry(f"+{x - 30}+{max(0, y - 76)}")

    # ------------------------------------------------------------ 状态机
    def play(self, name):
        if self.busy:
            return
        frames = load_frames(name)
        if not frames:
            return
        self.busy = True
        self.state = name
        self.action_frames = frames
        self.frame_i = 0
        self.label.place(x=0, y=0, width=W, height=H)
        self.say(random.choice(LINES.get(name, LINES["wave"])), 2400)
        self.tick()

    def tick(self):
        if not self.busy:
            return
        if self.frame_i >= len(self.action_frames):
            self.finish()
            return
        self.set_img(self.action_frames[self.frame_i])
        self.frame_i += 1
        self.root.after(FPS_MS, self.tick)

    def finish(self):
        self.busy = False
        self.state = "idle"
        self.set_img(self.idle_img)
        self.schedule_idle()

    def schedule_idle(self):
        # 自动化验收用：PET_NO_IDLE=1 关闭闲置动作，隔离出纯静态显示。
        if os.environ.get("PET_NO_IDLE"):
            return
        if getattr(self, "_idle_job", None):
            self.root.after_cancel(self._idle_job)
        delay = random.randint(9000, 15000)
        self._idle_job = self.root.after(
            delay, lambda: self.play(random.choice(("think", "sit"))) if not self.busy else None)

    # ------------------------------------------------------------ 交互
    def on_down(self, e):
        self.drag = {"x": e.x, "y": e.y, "moved": False}

    def on_move(self, e):
        if not self.drag:
            return
        x = self.root.winfo_x() + e.x - self.drag["x"]
        y = self.root.winfo_y() + e.y - self.drag["y"]
        self.drag["moved"] = True
        self.root.geometry(f"+{x}+{y}")
        self.follow_bubble()

    def on_up(self, e):
        if not self.drag:
            return
        moved = self.drag["moved"]
        self.drag = None
        if moved:
            self.save_pos()
            self.say(random.choice(LINES["drag"]), 1200)
            return
        # 延迟判定单击，给双击留窗口
        self.click_job = self.root.after(280, lambda: self.play("wave"))

    def on_double(self, e):
        if self.click_job:
            self.root.after_cancel(self.click_job)
            self.click_job = None
        self.play("jump")

    def on_right(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def quit(self):
        self.save_pos()
        self.root.destroy()

    def run(self):
        # 自动化验收用：设置 PET_AUTO_EXIT=<秒> 后自动退出并落盘位置，
        # 正常双击启动不设置该变量，行为完全不变。
        limit = os.environ.get("PET_AUTO_EXIT")
        if limit:
            try:
                sec = max(0.5, float(limit))
                self.root.after(int(sec * 1000), self.root.destroy)
            except ValueError:
                pass
        self.root.mainloop()


if __name__ == "__main__":
    os.chdir(HERE)
    Pet().run()
