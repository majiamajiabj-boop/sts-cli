"""Portable, DPI-aware Chinese advisor UI. Opening it never starts gameplay."""
import argparse
import ctypes
import json
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from assistant_paths import ROOT, data_dir, discover_games, installation, load_settings, save_settings
from assistant_advisor import AdviceSession
from assistant_runtime import ModeLock, start_advisor, start_auto

COLORS = {"background":"#181b20", "sidebar":"#121419", "panel":"#22262d",
          "text":"#f2f4f7", "secondary":"#c1c7d0", "muted":"#929ca9",
          "border":"#343a44", "button":"#2c323b", "hover":"#39414d",
          "accent":"#bacbdc", "success":"#8dbba3", "pending":"#b9aa8d"}


def enable_high_dpi():
    """Must run before Tk creates any HWND; the Python child owns its DPI mode."""
    if os.name != "nt":
        return
    try:
        function = ctypes.windll.user32.SetProcessDpiAwarenessContext
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_bool
        if function(ctypes.c_void_p(-4)):  # Per-monitor v2; no bitmap virtualization.
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def window_dpi(root):
    if os.name != "nt":
        return {"awareness": None, "dpi": round(root.winfo_fpixels("1i"))}
    u = ctypes.windll.user32
    try:
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.GetParent.restype = ctypes.c_void_p
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        u.GetWindowDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        return {"awareness": u.GetAwarenessFromDpiAwarenessContext(u.GetWindowDpiAwarenessContext(hwnd)), "dpi": u.GetDpiForWindow(hwnd)}
    except (AttributeError, OSError):
        return {"awareness": None, "dpi": round(root.winfo_fpixels("1i"))}


class AssistantApp:
    def __init__(self, root, *, state_path=None):
        self.root = root
        self.settings = load_settings()
        self.session = AdviceSession()
        self.state_path = Path(state_path or data_dir() / "advisor-state.json")
        self.child = self.mode = self.mode_lock = self.viewer = None
        self.compact = self.details_visible = self.settings_visible = False
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.full_geometry = f"{self.px(920)}x{self.px(620)}"
        root.title("尖塔助手 · 实时顾问")
        root.geometry(self.full_geometry)
        root.minsize(self.px(760), self.px(550))
        root.configure(bg=COLORS["background"])
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        self.configure_styles()
        self.sidebar = tk.Frame(root, bg=COLORS["sidebar"], width=self.px(164), padx=self.px(18), pady=self.px(26))
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.sidebar.rowconfigure(5, weight=1)
        tk.Label(self.sidebar, text="尖塔助手", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Microsoft YaHei UI", 17, "bold"), anchor="w").grid(row=0,column=0,sticky="ew",pady=(0,self.px(8)))
        tk.Label(self.sidebar, text="专注你的下一步", bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 9), anchor="w").grid(row=1,column=0,sticky="ew",pady=(0,self.px(36)))
        ttk.Label(self.sidebar,text="实时顾问",style="Selected.TLabel",padding=(self.px(14),self.px(12))).grid(row=2,column=0,sticky="ew",pady=(0,self.px(7)))
        self.auto_button = ttk.Button(self.sidebar,text="原有自动游玩",style="Nav.TButton",command=lambda:self.launch("auto"))
        self.auto_button.grid(row=3,column=0,sticky="ew",pady=(0,self.px(7)))
        ttk.Button(self.sidebar,text="战报查看",style="Nav.TButton",command=self.reports).grid(row=4,column=0,sticky="ew")
        ttk.Button(self.sidebar,text="游戏设置",style="Nav.TButton",command=self.toggle_settings).grid(row=6,column=0,sticky="ew",pady=(0,self.px(8)))
        ttk.Button(self.sidebar,text="使用说明",style="Nav.TButton",command=self.help).grid(row=7,column=0,sticky="ew")
        tk.Label(self.sidebar,text="本地建议\n无需 API Key",bg=COLORS["sidebar"],fg=COLORS["muted"],justify="left",font=("Microsoft YaHei UI",9),anchor="w").grid(row=8,column=0,sticky="ew",pady=(self.px(30),0))
        self.outer = ttk.Frame(root, padding=(self.px(30),self.px(26)))
        self.outer.grid(row=0,column=1,sticky="nsew")
        self.outer.columnconfigure(0,weight=1)
        self.outer.rowconfigure(3,weight=1)
        self.header = ttk.Frame(self.outer)
        self.header.grid(row=0,column=0,sticky="ew",pady=(0,self.px(24)))
        ttk.Label(self.header,text="实时顾问",style="Heading.TLabel").pack(side="left")
        self.compact_button = ttk.Button(self.header,text="精简悬浮窗  ↗",style="Quiet.TButton",command=self.toggle_compact)
        self.compact_button.pack(side="right")
        self.status = tk.StringVar(value="尚未连接游戏")
        self.status_label = ttk.Label(self.outer,textvariable=self.status,style="Muted.TLabel",wraplength=self.px(640))
        self.status_label.grid(row=1,column=0,sticky="ew",pady=(0,self.px(18)))
        self.hero = tk.Frame(self.outer,bg=COLORS["panel"],padx=self.px(26),pady=self.px(28))
        self.hero.grid(row=2,column=0,sticky="ew")
        self.hero.columnconfigure(0,weight=1)
        self.meta = tk.Frame(self.hero,bg=COLORS["panel"])
        self.meta.grid(row=0,column=0,sticky="ew",pady=(0,self.px(20)))
        self.scene_var = tk.StringVar()
        self.slot_var = tk.StringVar()
        tk.Label(self.meta,textvariable=self.scene_var,bg=COLORS["panel"],fg=COLORS["muted"],font=("Microsoft YaHei UI",10),anchor="w").pack(side="left")
        self.slot_label=tk.Label(self.meta,textvariable=self.slot_var,bg=COLORS["button"],fg=COLORS["text"],font=("Microsoft YaHei UI",10,"bold"),padx=self.px(10),pady=self.px(5))
        self.slot_label.pack(side="right")
        self.action_var=tk.StringVar(); self.target_var=tk.StringVar(); self.summary_var=tk.StringVar()
        self.action_label=tk.Label(self.hero,textvariable=self.action_var,bg=COLORS["panel"],fg=COLORS["text"],font=("Microsoft YaHei UI",26,"bold"),anchor="w",justify="left",wraplength=self.px(590))
        self.action_label.grid(row=1,column=0,sticky="ew",pady=(0,self.px(18)))
        self.target_label=tk.Label(self.hero,textvariable=self.target_var,bg=COLORS["panel"],fg=COLORS["secondary"],font=("Microsoft YaHei UI",13),anchor="w",justify="left",wraplength=self.px(590))
        self.target_label.grid(row=2,column=0,sticky="ew",pady=(0,self.px(24)))
        self.summary_label=tk.Label(self.hero,textvariable=self.summary_var,bg=COLORS["panel"],fg=COLORS["muted"],font=("Microsoft YaHei UI",11),anchor="w",justify="left",wraplength=self.px(590))
        self.summary_label.grid(row=3,column=0,sticky="ew",pady=(0,self.px(8)))
        self.details_button=tk.Button(self.hero,text="查看判断依据  +",command=self.toggle_details,bg=COLORS["panel"],fg=COLORS["secondary"],activebackground=COLORS["panel"],activeforeground=COLORS["text"],font=("Microsoft YaHei UI",10),relief="flat",bd=0,highlightthickness=0,anchor="w",cursor="hand2")
        self.details_button.grid(row=4,column=0,sticky="w",pady=(self.px(12),0))
        self.details=tk.Frame(self.hero,bg=COLORS["panel"])
        self.details.grid(row=5,column=0,sticky="ew",pady=(self.px(12),0))
        self.advice=tk.Text(self.details,bg=COLORS["panel"],fg=COLORS["muted"],font=("Microsoft YaHei UI",10),wrap="word",height=4,width=1,bd=0,highlightthickness=0,selectbackground=COLORS["button"])
        self.advice.pack(side="left",fill="both",expand=True)
        scroll=ttk.Scrollbar(self.details,command=self.advice.yview)
        scroll.pack(side="right",fill="y")
        self.advice.configure(yscrollcommand=scroll.set)
        self.details.grid_remove()
        self.steps=ttk.Frame(self.outer)
        self.steps.grid(row=3,column=0,sticky="new",pady=(self.px(24),0))
        for i,(number,title,description) in enumerate((("01","连接游戏","从助手启动游戏"),("02","按你的节奏玩","你来出牌与选择"),("03","随时看建议","每次操作后更新"))):
            cell=ttk.Frame(self.steps);cell.grid(row=0,column=i,sticky="nw",padx=(0,self.px(24)))
            ttk.Label(cell,text=number,style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell,text=title,style="Step.TLabel").pack(anchor="w",pady=(self.px(6),self.px(4)))
            ttk.Label(cell,text=description,style="Muted.TLabel").pack(anchor="w")
        self.footer=ttk.Frame(self.outer)
        self.footer.grid(row=4,column=0,sticky="ew",pady=(self.px(24),0))
        self.footer.columnconfigure(0,weight=1)
        games=discover_games()
        self.game=tk.StringVar(value=self.settings.get("game_dir",str(games[0]) if games else ""))
        self.installation_hint=tk.StringVar(value="已找到 Slay the Spire" if self.game.get() else "首次使用，请配置游戏位置")
        ttk.Label(self.footer,textvariable=self.installation_hint,style="Muted.TLabel").grid(row=0,column=0,sticky="w")
        ttk.Button(self.footer,text="配置游戏",style="Quiet.TButton",command=self.toggle_settings).grid(row=1,column=0,sticky="w",pady=(self.px(4),0))
        self.advisor_button=ttk.Button(self.footer,text="启动实时顾问",style="Primary.TButton",command=lambda:self.launch("advisor"))
        self.advisor_button.grid(row=0,column=1,rowspan=2,sticky="e")
        self.dependency=tk.StringVar(value="支持 Steam 自动识别，也可手动选择游戏与 Mod。")
        self.settings_window=None
        self.last_render=object()
        self.render(None)
        self.hero.bind("<Configure>",self.resize_advice)
        root.protocol("WM_DELETE_WINDOW",self.close)
        root.after(100,self.tick)

    def px(self,value):
        return round(value*self.scale)

    def configure_styles(self):
        s=ttk.Style();s.theme_use("clam")
        s.configure("TFrame",background=COLORS["background"])
        s.configure("TLabel",background=COLORS["background"],foreground=COLORS["text"],font=("Microsoft YaHei UI",10))
        s.configure("Muted.TLabel",foreground=COLORS["muted"],font=("Microsoft YaHei UI",9))
        s.configure("Heading.TLabel",font=("Microsoft YaHei UI",19,"bold"))
        s.configure("Step.TLabel",font=("Microsoft YaHei UI",10,"bold"))
        s.configure("Selected.TLabel",background=COLORS["panel"],foreground=COLORS["text"],font=("Microsoft YaHei UI",11,"bold"))
        s.configure("TButton",background=COLORS["button"],foreground=COLORS["secondary"],borderwidth=0,relief="flat",padding=(self.px(12),self.px(10)),font=("Microsoft YaHei UI",10),anchor="center",lightcolor=COLORS["button"],darkcolor=COLORS["button"],bordercolor=COLORS["button"])
        s.map("TButton",background=[("disabled",COLORS["panel"]),("active",COLORS["hover"])],foreground=[("disabled",COLORS["muted"]),("active",COLORS["text"])])
        for name,bg in (("Nav",COLORS["sidebar"]),("Quiet",COLORS["background"])):
            s.configure(name+".TButton",background=bg,lightcolor=bg,darkcolor=bg,bordercolor=bg,anchor="w")
        s.configure("Primary.TButton",background=COLORS["text"],foreground="#1b2027",font=("Microsoft YaHei UI",11,"bold"),lightcolor=COLORS["text"],darkcolor=COLORS["text"],bordercolor=COLORS["text"])
        s.map("Primary.TButton",background=[("disabled",COLORS["button"]),("active","#dbe2eb")],foreground=[("disabled",COLORS["muted"]),("active","#1b2027")])
        s.configure("TEntry",fieldbackground=COLORS["panel"],foreground=COLORS["text"],insertcolor=COLORS["text"],padding=self.px(8),bordercolor=COLORS["border"])
        s.configure("Vertical.TScrollbar",background=COLORS["button"],troughcolor=COLORS["panel"],arrowcolor=COLORS["muted"],bordercolor=COLORS["panel"])

    def resize_advice(self,event):
        width=max(self.px(210),event.width-self.px(52))
        for w in (self.action_label,self.target_label,self.summary_label):
            w.configure(wraplength=width)
        self.status_label.configure(wraplength=event.width)

    def toggle_settings(self,visible=None):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        if visible is False:
            return
        self.settings_visible=True
        dialog=self.settings_window=tk.Toplevel(self.root)
        dialog.title("游戏设置 · 尖塔助手")
        dialog.configure(bg=COLORS["background"])
        dialog.transient(self.root)
        frame=ttk.Frame(dialog,padding=self.px(26));frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="游戏与通信",style="Heading.TLabel").pack(anchor="w",pady=(0,self.px(20)))
        ttk.Label(frame,text="Slay the Spire 安装目录",style="Muted.TLabel").pack(anchor="w")
        row=ttk.Frame(frame);row.pack(fill="x",pady=(self.px(8),self.px(14)))
        ttk.Entry(row,textvariable=self.game,width=48).pack(side="left",fill="x",expand=True)
        ttk.Button(row,text="选择文件夹",command=self.choose_game).pack(side="right",padx=(self.px(10),0))
        ttk.Label(frame,textvariable=self.dependency,wraplength=self.px(550),style="Muted.TLabel").pack(fill="x",pady=(0,self.px(20)))
        row=ttk.Frame(frame);row.pack(fill="x")
        ttk.Button(row,text="检查依赖",command=self.check).pack(side="left")
        ttk.Button(row,text="手动选择 Mod",command=self.choose_mods).pack(side="left",padx=self.px(8))
        ttk.Button(row,text="完成",command=dialog.destroy).pack(side="right")

    def ensure_content_fits(self):
        if not self.root.winfo_ismapped() or self.root.winfo_width()<=1:
            return
        self.root.update_idletasks()
        needed=self.outer.winfo_reqheight()
        limit=self.root.winfo_screenheight()-self.px(64)
        height=min(needed,limit)
        if self.root.winfo_height()<height:
            self.root.geometry(f"{self.root.winfo_width()}x{height}")

    def toggle_details(self):
        self.details_visible=not self.details_visible
        if self.details_visible:self.details.grid()
        else:self.details.grid_remove()
        self.details_button.configure(text="收起判断依据  −" if self.details_visible else "查看判断依据  +")
        self.root.after_idle(self.ensure_content_fits)

    def toggle_compact(self):
        self.compact=not self.compact
        if self.compact:
            self.full_geometry=self.root.geometry()
            self.sidebar.grid_remove();self.steps.grid_remove();self.footer.grid_remove()
            self.outer.configure(padding=self.px(18))
            self.hero.configure(padx=self.px(18),pady=self.px(20))
            self.action_label.configure(font=("Microsoft YaHei UI",21,"bold"))
            self.compact_button.configure(text="完整窗口  ↙")
            self.root.minsize(self.px(340),self.px(360))
            self.root.geometry(f"{self.px(400)}x{self.px(430)}")
            if self.details_visible:self.toggle_details()
        else:
            self.sidebar.grid();self.footer.grid()
            if self.session.advice is None:self.steps.grid()
            self.outer.configure(padding=(self.px(30),self.px(26)))
            self.hero.configure(padx=self.px(26),pady=self.px(28))
            self.action_label.configure(font=("Microsoft YaHei UI",26,"bold"))
            self.compact_button.configure(text="精简悬浮窗  ↗")
            self.root.minsize(self.px(760),self.px(550))
            self.root.geometry(self.full_geometry)
        self.root.attributes("-topmost",self.compact)

    def config(self):
        self.settings["game_dir"] = self.game.get().strip()
        save_settings(self.settings)
        return dict(self.settings)

    def choose_game(self):
        value = filedialog.askdirectory(title="选择包含 desktop-1.0.jar 的游戏文件夹")
        if value:
            self.game.set(value)
            self.check()

    def choose_mods(self):
        for name, key in (("ModTheSpire", "modthespire"), ("BaseMod", "basemod")):
            value = filedialog.askopenfilename(title=f"选择 {name}.jar（取消可保留当前设置）", filetypes=[("Java Mod", "*.jar")])
            if value:
                self.settings[key] = value
        self.check()

    def check(self):
        try:
            info = installation(settings=self.config())
            errors = info.errors()
            self.dependency.set("\n".join(errors) if errors else "依赖检查通过：游戏、Java、ModTheSpire、BaseMod 均已找到。")
        except Exception as exc:
            self.dependency.set(f"配置无法保存或读取：{exc}")

    def launch(self, mode):
        if self.mode is not None:
            messagebox.showinfo("模式互斥", "本助手已启动一个模式，请先手动退出游戏并关闭助手后再切换。")
            return
        title = "启动实时顾问" if mode == "advisor" else "启动自动游玩"
        description = "将安装通信 Mod 并启动游戏。已有 Mod 文件会保留备份。\n" + ("你手动操作，助手不会替你点击或出牌。" if mode == "advisor" else "程序将自动操作游戏并开始一局 fast-policy-v5 对局。")
        if not messagebox.askokcancel(title, description):
            return
        try:
            self.mode_lock = ModeLock().acquire()
            self.child = (start_advisor if mode == "advisor" else start_auto)(self.config())
            self.mode = mode
            self.advisor_button.configure(state="disabled")
            self.auto_button.configure(state="disabled")
            self.status.set("正在启动游戏，请等待 Mod 加载。")
        except Exception as exc:
            if self.mode_lock:
                self.mode_lock.close()
            self.mode_lock = None
            messagebox.showerror("未启动游戏", str(exc))

    def render(self,advice):
        value=(None,self.session.status) if advice is None else (advice.token,advice.action,advice.target,advice.reason,advice.scene)
        if value==self.last_render:return
        self.last_render=value
        self.advice.configure(state="normal");self.advice.delete("1.0","end")
        if advice:
            match=re.search(r"（手牌 (\d+)）$",advice.action)
            self.action_var.set(advice.action[:match.start()] if match else advice.action)
            self.scene_var.set(advice.scene+" / 当前建议")
            self.slot_var.set("手牌 "+match.group(1) if match else "")
            self.target_var.set("目标  ·  "+advice.target if advice.target!="无" else "无需选择目标")
            if advice.target!="无":self.target_label.grid()
            else:self.target_label.grid_remove()
            self.summary_var.set(advice.reason.split("\n",1)[0])
            self.advice.insert("end",advice.reason)
            self.details_button.grid();self.steps.grid_remove()
        else:
            self.scene_var.set("由你掌控每一步")
            status=self.session.status
            title="等待回合结算" if "正在结算" in status else ("正在分析下一步" if "正在计算" in status else ("暂时没有可用建议" if "建议暂不可用" in status else "等待游戏连接"))
            self.action_var.set(title)
            self.target_var.set("尚无当前目标");self.slot_var.set("")
            self.target_label.grid_remove()
            if "正在结算" in status:
                self.summary_var.set("结算结束后，将自动显示新的建议。")
            elif "正在计算" in status:
                self.summary_var.set("正在根据当前状态计算建议，请稍候。")
            else:
                self.summary_var.set("启动游戏后，建议会随你的每次操作自动更新。\n出牌与选择，始终由你决定。")
            if self.details_visible:self.toggle_details()
            self.details_button.grid_remove()
            if not self.compact:self.steps.grid()
        if self.slot_var.get():self.slot_label.pack(side="right")
        else:self.slot_label.pack_forget()
        self.advice.configure(state="disabled")
        self.root.after_idle(self.ensure_content_fits)

    def tick(self):
        if self.mode!="auto":
            self.session.tick(self.state_path)
            self.status.set(self.session.status)
            self.render(self.session.advice)
            self.status_label.configure(foreground=COLORS["success"] if self.session.advice else COLORS["muted"])
        elif self.child:
            code=self.child.poll()
            self.status.set("自动模式启动检查中" if code is None else ("自动模式启动器已完成；请通过战报查看进度。" if code==0 else "自动模式启动失败，请查看 data/auto-start.log。"))
        self.root.after(100,self.tick)

    def reports(self):
        try:
            from viewer.server import build_server
            import webbrowser
            if self.viewer is None:
                self.viewer = build_server(ROOT, port=0)
                threading.Thread(target=self.viewer.serve_forever, daemon=True).start()
            webbrowser.open(f"http://127.0.0.1:{self.viewer.server_port}")
        except Exception as exc:
            messagebox.showerror("战报无法打开", str(exc))

    def help(self):
        messagebox.showinfo("最短使用步骤", "1. 在 Steam 安装游戏，订阅 ModTheSpire 与 BaseMod。\n2. 完整解压助手，选择游戏目录并检查依赖。\n3. 退出已有游戏，点击启动实时顾问。\n4. 手动开始一局，观察建议随每次操作刷新。\n\n支持铁甲战士、静默猎手、故障机器人。\n详细说明见发布包内 WINDOWS_ASSISTANT_README.md。")

    def close(self):
        if self.child and (self.mode == "auto" or self.child.poll() is None):
            messagebox.showinfo("请先退出游戏", "为保持模式互斥，请先手动退出游戏。助手不会强制终止游戏。")
            if self.mode == "auto":
                from assistant_runtime import running_game_processes
                try:
                    if running_game_processes():
                        return
                except Exception:
                    return
            else:
                return
        if self.mode_lock:
            self.mode_lock.close()
        if self.viewer:
            self.viewer.shutdown()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, help="只读回放验证用状态文件，不启动游戏")
    parser.add_argument("--smoke-test", type=Path, help="创建真实窗口并输出控件自检记录后退出")
    parser.add_argument("--smoke-missing-dependencies", action="store_true")
    parser.add_argument("--compact", action="store_true", help="以精简置顶窗口打开，只读建议更醒目")
    args = parser.parse_args()
    enable_high_dpi()
    root = tk.Tk()
    app = AssistantApp(root, state_path=args.state_file)
    if args.compact:
        app.toggle_compact()
    if args.smoke_test:
        if args.smoke_missing_dependencies:
            app.game.set(str(data_dir() / "不存在的 游戏目录"))
            app.settings.update(modthespire=str(data_dir() / "missing-MTS.jar"), basemod=str(data_dir() / "missing-BaseMod.jar"))
            app.check()
            app.toggle_settings()
        def finish():
            root.update_idletasks()
            result = {"title": root.title(), "geometry": root.geometry(), "tk": root.tk.call("info", "patchlevel"), "python": sys.version.split()[0], "executable": sys.executable, "source_root": str(ROOT), "game_started": app.child is not None, "controls": [app.advisor_button.cget("text"), app.auto_button.cget("text")], "status": app.status.get(), "dependencies": app.dependency.get(), "advice": app.advice.get("1.0", "end-1c"), "action": app.action_var.get(), "target": app.target_var.get(), "slot": app.slot_var.get(), "scene": app.scene_var.get(), "compact": app.compact, "dpi": window_dpi(root)}
            args.smoke_test.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            root.destroy()
        root.after(2500, finish)
    root.mainloop()


if __name__ == "__main__":
    main()
