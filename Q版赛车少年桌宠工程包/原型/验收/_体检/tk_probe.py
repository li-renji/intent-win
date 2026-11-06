# -*- coding: utf-8 -*-
"""验证 tkinter + 真 RGBA PNG + transparentcolor 能否做到真透明且角色点得到。

品红 #FF00FF 作为透明色：窗口与 Label 背景都是品红，PNG 的透明区露出品红
被系统抠成透明并让鼠标穿透；PNG 的角色像素不是品红，正常显示且可点击。
"""
import os
import threading
import time
import ctypes
import tkinter as tk
from PIL import Image, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = r"D:/INTENT-win/plugins/ajue/assets/pet_main_transparent.png"
X, Y, W, H = 300, 200, 240, 240
KEY = "#FF00FF"
clicks = [0]


def diag(root):
    time.sleep(4)
    from PIL import ImageGrab
    out = os.path.join(HERE, "probe_tk.png")
    ImageGrab.grab(bbox=(X - 20, Y - 20, X + W + 20, Y + H + 20)).save(out)
    print("[探针] 截图:", out, flush=True)
    u = ctypes.windll.user32
    old = (ctypes.c_long * 2)()
    u.GetCursorPos(ctypes.byref(old))
    u.SetCursorPos(X + 120, Y + 120)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    u.SetCursorPos(old[0], old[1])
    time.sleep(0.5)
    print("[探针] 点击角色后计数:", clicks[0], flush=True)
    root.after(200, root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.config(bg=KEY)
    root.geometry(f"{W}x{H}+{X}+{Y}")

    im = Image.open(PNG).convert("RGBA")
    bbox = im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    im = im.crop(bbox)
    im.thumbnail((200, 200), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(im)

    label = tk.Label(root, image=photo, bg=KEY, bd=0)
    label.place(x=(W - im.width) // 2, y=(H - im.height) // 2)
    label.bind("<Button-1>", lambda e: clicks.__setitem__(0, clicks[0] + 1))

    threading.Thread(target=diag, args=(root,), daemon=True).start()
    root.mainloop()
