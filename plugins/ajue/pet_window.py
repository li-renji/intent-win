import tkinter as tk
from PIL import Image, ImageTk
import os
import random

# 桌宠资源路径
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

class DesktopPet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("阿觉桌宠")
        
        # 窗口设置：无边框、置顶、工具窗口（不在任务栏显示）
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-toolwindow", True)
        
        # 关键：把白色变成透明色，这样白底的图片/视频就只有角色可见
        self.root.attributes("-transparentcolor", "white")
        self.root.config(bg="white")
        
        # 初始位置：屏幕右下角
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"180x220+{screen_w - 200}+{screen_h - 300}")
        
        # 显示图片的Label
        self.label = tk.Label(self.root, bg="white")
        self.label.pack()
        
        # 加载默认待机图片
        self.current_action = "idle"
        self.frames = []
        self.frame_idx = 0
        self.load_action("idle")
        
        # 拖拽相关
        self.drag_data = {"x": 0, "y": 0}
        self.label.bind("<ButtonPress-1>", self.on_drag_start)
        self.label.bind("<B1-Motion>", self.on_drag_motion)
        self.label.bind("<Double-Button-1>", self.on_click)
        self.label.bind("<Button-3>", self.on_right_click)
        
        # 右键菜单
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开对话", command=self.open_chat)
        self.menu.add_command(label="互动一下", command=self.random_action)
        self.menu.add_separator()
        self.menu.add_command(label="让阿觉休息", command=self.root.quit)
        
        # 开始动画循环
        self.update_animation()
        
    def load_action(self, action_name):
        """加载对应动作的帧序列"""
        self.current_action = action_name
        self.frames = []
        
        # 待机动作用静态图
        if action_name == "idle":
            img = Image.open(os.path.join(ASSETS_DIR, "pet_main.png"))
            img = img.resize((160, 180), Image.Resampling.LANCZOS)
            self.frames.append(ImageTk.PhotoImage(img))
        else:
            # 其他动作先加载对应视频的第一帧，后续可以扩展成帧序列
            # 现在先用静态图代替，后续再做视频帧提取
            img = Image.open(os.path.join(ASSETS_DIR, "pet_main.png"))
            img = img.resize((160, 180), Image.Resampling.LANCZOS)
            self.frames.append(ImageTk.PhotoImage(img))
        
        self.frame_idx = 0
    
    def update_animation(self):
        """动画循环"""
        if self.frames:
            self.label.config(image=self.frames[self.frame_idx])
            self.frame_idx = (self.frame_idx + 1) % len(self.frames)
        
        # 呼吸浮动效果
        self.root.update_idletasks()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        # 轻微上下浮动
        offset = int((self.root.tk.call('clock', 'milliseconds') / 500) % 2)
        self.root.geometry(f"180x220+{x}+{y + offset}")
        
        self.root.after(100, self.update_animation)
    
    def on_drag_start(self, event):
        self.drag_data["x"] = event.x
        self.drag_data["y"] = event.y
    
    def on_drag_motion(self, event):
        x = self.root.winfo_x() + event.x - self.drag_data["x"]
        y = self.root.winfo_y() + event.y - self.drag_data["y"]
        self.root.geometry(f"+{x}+{y}")
    
    def on_click(self, event):
        self.random_action()
    
    def on_right_click(self, event):
        self.menu.post(event.x_root, event.y_root)
    
    def random_action(self):
        """随机播放一个动作"""
        actions = ["wave", "jump", "think", "sit"]
        action = random.choice(actions)
        self.load_action(action)
        # 5秒后切回待机
        self.root.after(5000, lambda: self.load_action("idle"))
    
    def open_chat(self):
        """打开对话面板，这里后续对接原来的pywebview窗口"""
        print("打开对话面板")
    
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    pet = DesktopPet()
    pet.run()
