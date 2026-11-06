# -*- coding: utf-8 -*-
"""验证 v2：RGBA 预合成到纯品红底再交给 Tk，避免 PhotoImage 的 alpha 合成异常。"""
import os
import threading
import time
import ctypes
import tkinter as tk
from PIL import Image, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = r"D:/INTENT-win/plugins/ajue/assets/pet_main_transparent.png"
X, Y, W, H = 300, 200, 240, 240
KEY = (255, 0, 255)
clicks = [0]


def diag(root):
    time.sleep(4)
    from PIL import ImageGrab
    out = os.path.join(HERE, "probe_tk2.png")
    ImageGrab.grab(bbox=(X - 20, Y - 20, X + W + 20, Y + H + 20)).save(out)
    full = os.path.join(HERE, "probe_tk2_full.png")
    ImageGrab.grab().save(full)
    print("[探针] 局部截图:", out, flush=True)
    print("[探针] 全屏截图:", full, flush=True)
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
    root.attributes("-transparentcolor", "#FF00FF")
    root.config(bg="#FF00FF")
    root.geometry(f"{W}x{H}+{X}+{Y}")

    im = Image.open(PNG).convert("RGBA")
    bbox = im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    im = im.crop(bbox)
    im.thumbnail((200, 200), Image.Resampling.LANCZOS)
    # 关键：alpha 二值化后预合成到品红底，得到不含 alpha 的 RGB 图
    a = im.getchannel("A").point(lambda v: 255 if v > 128 else 0)
    flat = Image.new("RGB", im.size, KEY)
    flat.paste(im.convert("RGB"), (0, 0), a)
    flat.save(os.path.join(HERE, "probe_flat.png"))
    photo = ImageTk.PhotoImage(flat)

    label = tk.Label(root, image=photo, bg="#FF00FF", bd=0)
    label.place(x=(W - flat.width) // 2, y=(H - flat.height) // 2)
    label.bind("<Button-1>", lambda e: clicks.__setitem__(0, clicks[0] + 1))

    threading.Thread(target=diag, args=(root,), daemon=True).start()
    root.mainloop()
