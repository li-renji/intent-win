import tkinter as tk
from PIL import Image, ImageTk
import os
import random

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")

class QidianPet:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("奇点桌宠")
        
        # 窗口核心设置
        self.root.overrideredirect(True)  # 无边框
        self.root.attributes("-topmost", True)  # 置顶
        self.root.attributes("-toolwindow", True)  # 不在任务栏显示
        
        # 关键：把白色变成完全透明，只有角色可见
        self.root.attributes("-transparentcolor", "white")
        self.root.config(bg="white")
        
        # 初始大小和位置
        self.pet_size = 150
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.pet_size}x{self.pet_size}+{screen_w - 200}+{screen_h - 300}")
        
        # 显示角色的Label
        self.label = tk.Label(self.root, bg="white")
        self.label.pack(expand=True)
        
        # 加载素材
        self.current_action = "idle"
        self.load_assets()
        
        # 交互绑定
        self.drag_data = {"x": 0, "y": 0}
        self.label.bind("<ButtonPress-1>", self.on_drag_start)
        self.label.bind("<B1-Motion>", self.on_drag_motion)
        self.label.bind("<Double-Button-1>", self.on_interact)
        self.label.bind("<Button-3>", self.show_menu)
        
        # 右键菜单
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开对话", command=self.open_chat)
        self.menu.add_command(label="互动一下", command=self.random_action)
        self.menu.add_separator()
        self.menu.add_command(label="让桌宠休息", command=self.root.quit)
        
        # 开始动画循环
        self.update_animation()
        
    def load_assets(self):
        """加载所有动作的素材"""
        self.images = {}
        
        # 待机
        idle_img = Image.open(os.path.join(ASSETS_DIR, "pet_main.png"))
        idle_img = idle_img.resize((self.pet_size, self.pet_size), Image.Resampling.LANCZOS)
        self.images["idle"] = ImageTk.PhotoImage(idle_img)
        
        # 其他动作暂时用待机图占位，后续替换成动作帧
        for action in ["wave", "jump", "think", "sit"]:
            self.images[action] = self.images["idle"]
            
        self.label.config(image=self.images["idle"])
        
    def update_animation(self):
        """动画循环：轻微浮动"""
        # 呼吸浮动效果
        offset = int((self.root.tk.call('clock', 'milliseconds') / 400) % 2) * 2
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry(f"{self.pet_size}x{self.pet_size}+{x}+{y + offset}")
        
        self.root.after(50, self.update_animation)
        
    def on_drag_start(self, event):
        self.drag_data["x"] = event.x
        self.drag_data["y"] = event.y
        
    def on_drag_motion(self, event):
        x = self.root.winfo_x() + event.x - self.drag_data["x"]
        y = self.root.winfo_y() + event.y - self.drag_data["y"]
        self.root.geometry(f"+{x}+{y}")
        
    def on_interact(self, event):
        self.random_action()
        
    def random_action(self):
        """随机播放一个动作"""
        actions = ["wave", "jump", "think", "sit"]
        action = random.choice(actions)
        self.label.config(image=self.images[action])
        # 2秒后切回待机
        self.root.after(2000, lambda: self.label.config(image=self.images["idle"]))
        
    def show_menu(self, event):
        self.menu.post(event.x_root, event.y_root)
        
    def open_chat(self):
        """打开对话面板，后续对接原来的pywebview窗口"""
        print("打开对话面板")
        
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    pet = QidianPet()
    pet.run()
