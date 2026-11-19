# -*- coding: utf-8 -*-
"""Word 超链接关键字扫描器 —— GUI 入口"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, ttk

import scanner


def _set_app_user_model_id():
    """在窗口创建之前设置显式 AppUserModelID。

    Windows 任务栏按钮图标按 AppUserModelID 缓存。若不显式设置，任务栏会
    沿用同一默认 ID 下旧版本遗留的羽毛图标；该调用必须在任何 tkinter 窗口
    创建之前执行，因此在模块顶层调用。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [wintypes.LPCWSTR]
        fn.restype = None
        fn("WordLinkScanner.ForestAnt.2026.11")
    except Exception:
        pass


_set_app_user_model_id()

APP_TITLE = "Word 超链接关键字扫描器"
COLS = ("idx", "page", "level", "status", "matched", "text", "title", "url",
        "via", "note")
HEADERS = {"idx": "#", "page": "页码", "level": "来源", "status": "状态",
           "matched": "命中关键字", "text": "链接文字", "title": "网页标题",
           "url": "链接 URL", "via": "方式", "note": "备注"}
WIDTHS = {"idx": 46, "page": 62, "level": 70, "status": 70, "matched": 160,
          "text": 200, "title": 190, "url": 340, "via": 90, "note": 180}

# 抓取方式 / 浏览器通道：界面文案 -> 后端枚举（scanner.RENDER_MODES / BROWSER_CHANNELS）
MODE_OPTIONS = ("智能（HTTP 失败自动用浏览器）", "仅 HTTP（快，旧版行为）", "全部浏览器渲染")
CHANNEL_OPTIONS = ("自动", "Microsoft Edge", "Google Chrome", "Playwright Chromium")
MODE_MAP = {"智能（HTTP 失败自动用浏览器）": "auto",
            "仅 HTTP（快，旧版行为）": "http",
            "全部浏览器渲染": "browser"}
CHANNEL_MAP = {"自动": "auto", "Microsoft Edge": "msedge",
               "Google Chrome": "chrome", "Playwright Chromium": "chromium"}
MODE_LABEL = {"auto": "智能", "http": "仅 HTTP", "browser": "全部浏览器渲染"}


def _setup_window_icon(window):
    """给窗口设置森林蚂蚁图标，并尽量避开 Windows 任务栏的旧图标缓存。

    仅在 Windows 上启用 Win32 增强；任何失败都写入 %TEMP%/wls_icon.log，
    不再用裸 except 静默吞掉，便于诊断。返回 (成功与否, 说明文字)。
    """
    import sys

    def _write_log(text):
        try:
            log_path = os.path.join(os.environ.get("TEMP", "/tmp"), "wls_icon.log")
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass

    # 1) 解析图标路径：冻结环境优先 sys._MEIPASS，再回退脚本目录 / cwd。
    #    Windows 用 .ico；macOS 的 tkinter 不支持 iconbitmap，改用 .png + iconphoto。
    is_mac = (os.name == "posix" and sys.platform == "darwin")
    ext = "png" if is_mac else "ico"
    fname = "forest_ant_window.png" if is_mac else "forest_ant.ico"
    bases = []
    if getattr(sys, "frozen", False):
        mei = getattr(sys, "_MEIPASS", "") or ""
        if mei:
            bases.append(os.path.join(mei, "assets"))
    bases.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"))
    bases.append(os.path.join(os.getcwd(), "assets"))
    candidates = [os.path.join(b, fname) for b in bases]
    # PNG 兜底（源码运行 / 打包漏资源时）
    if is_mac:
        candidates += [os.path.join(b, "forest_ant_window.png") for b in bases]
    icon_path = next((p for p in candidates if p and os.path.isfile(p)), "")
    lines = ["platform=%s" % sys.platform,
             "frozen=%s" % getattr(sys, "frozen", False),
             "MEIPASS=%s" % getattr(sys, "_MEIPASS", ""),
             "candidates=%s" % candidates,
             "chosen=%s" % icon_path]
    if not icon_path:
        _write_log("\n".join(lines + ["RESULT=FAIL: icon not found"]))
        return False, "未找到图标文件 %s" % fname

    # 2) Tk 层设置（标题栏 / 默认窗口图标）：Windows=iconbitmap，macOS=iconphoto
    tk_ok = False
    tk_err = ""
    if is_mac:
        try:
            img = tk.PhotoImage(file=icon_path)
            window.iconphoto(True, img)
            window._forest_ant_icon = img  # 持有引用，防止被 GC 后图标失效
            tk_ok = True
        except Exception as exc:
            tk_err = "%s: %s" % (type(exc).__name__, exc)
        lines.append("iconphoto=%s %s" % (tk_ok, tk_err))
    else:
        try:
            window.iconbitmap(icon_path)
            tk_ok = True
        except Exception as exc:
            tk_err = "%s: %s" % (type(exc).__name__, exc)
        lines.append("iconbitmap=%s %s" % (tk_ok, tk_err))

    # 3) Win32 层：新的 AppUserModelID + 显式大/小图标 + 类图标 + Shell 刷新。
    #    Windows 任务栏按钮图标按 AppUserModelID 缓存，换一个新的 ID 可让
    #    任务栏重新按 exe 资源读取图标，绕开旧版本遗留的羽毛图标缓存。
    win_ok = False
    win_err = ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32

            # AppUserModelID 已在模块导入时、窗口创建之前设置（见 _set_app_user_model_id）
            lines.append("AppUserModelID=set at import")

            hwnd = int(window.winfo_id())
            lines.append("hwnd=%s" % hwnd)

            fn_load = user32.LoadImageW
            fn_load.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
            fn_load.restype = wintypes.HANDLE
            fn_send = user32.SendMessageW
            fn_send.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
            fn_send.restype = ctypes.c_ssize_t
            fn_class = user32.SetClassLongPtrW
            fn_class.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            fn_class.restype = ctypes.c_ulonglong

            image_icon, load_from_file = 1, 0x0010
            wm_seticon, icon_small, icon_big = 0x0080, 0, 1
            gcl_hicon, gcl_hiconsm = -14, -34
            h16 = fn_load(None, icon_path, image_icon, 16, 16, load_from_file)
            h32 = fn_load(None, icon_path, image_icon, 32, 32, load_from_file)
            if not h16 or not h32:
                raise RuntimeError("LoadImageW 失败 h16=%s h32=%s" % (h16, h32))
            # 大图标给 Alt+Tab / 任务栏大按钮，小图标给标题栏 / 任务栏小按钮
            fn_send(hwnd, wm_seticon, icon_big, h32)
            fn_send(hwnd, wm_seticon, icon_small, h16)
            # 窗口类图标：Tk 默认注册的是羽毛，必须一并覆盖
            fn_class(hwnd, gcl_hicon, h32)
            fn_class(hwnd, gcl_hiconsm, h16)
            lines.append("LoadImage h16=%s h32=%s" % (h16, h32))
            lines.append("WM_SETICON(class icons)=ok")

            # 通知 Shell 关联发生变化，促使 Explorer 重新读取图标
            fn_notify = shell32.SHChangeNotify
            fn_notify.argtypes = [ctypes.c_long, wintypes.UINT,
                                  ctypes.c_void_p, ctypes.c_void_p]
            fn_notify.restype = None
            fn_notify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED / SHCNF_IDLIST
            lines.append("SHChangeNotify=ok")
            win_ok = True
        except Exception as exc:
            win_err = "%s: %s" % (type(exc).__name__, exc)

    lines.append("Win32=%s %s" % (win_ok, win_err))
    lines.append("RESULT=%s" % ("OK" if tk_ok else "FAIL"))
    _write_log("\n".join(lines))
    return tk_ok, "; ".join(lines)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # 先隐藏窗口：Windows 任务栏会在窗口首次显示时抓取并缓存图标，
        # 若此时还是 Tk 默认羽毛，之后再用 WM_SETICON 改也不会更新任务栏。
        # 因此先 withdraw，等森林蚂蚁图标全部设置完成后再 deiconify。
        self.withdraw()
        self.title(APP_TITLE)
        self.geometry("1160x780")
        self.minsize(960, 640)
        # 窗口/任务栏图标：森林蚂蚁（兼容 PyInstaller 冻结环境与源码运行）
        self._icon_ok, self._icon_detail = _setup_window_icon(self)

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 9))
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._running = False
        self._detecting = False
        self._all_rows: list[dict] = []
        self.file_var = tk.StringVar()
        self._only_hits = tk.BooleanVar(value=True)
        self._ignore_case = tk.BooleanVar(value=True)
        self._skip_ssl = tk.BooleanVar(value=False)
        self.workers = tk.IntVar(value=8)
        self.timeout = tk.IntVar(value=10)
        self.max_pages = tk.IntVar(value=1)
        # 浏览器渲染参数：StringVar 便于 Spinbox 手输后在 _start 统一收敛
        self.render_mode = tk.StringVar(value=MODE_OPTIONS[0])
        self.browser_channel = tk.StringVar(value=CHANNEL_OPTIONS[0])
        self.render_wait = tk.StringVar(value="2")
        self.browser_slots = tk.StringVar(value="2")
        # 详情页跟进：默认不勾选（用户要求），需要时手动勾选，每页最多 10 条
        self.follow_details = tk.BooleanVar(value=False)
        self.max_details = tk.StringVar(value="10")
        self.progress_var = tk.StringVar(value="就绪")
        self.status_var = tk.StringVar(value="提示：双击表格任意一行可在浏览器中打开该链接")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain)
        # 图标与 UI 均就绪后再显示窗口，让任务栏首次抓取即为森林蚂蚁
        self.deiconify()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(6, weight=1)

        ttk.Label(frm, text="Word 文档：").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frm, textvariable=self.file_var).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(frm, text="浏览...", command=self._choose).grid(row=0, column=2, padx=6)

        ttk.Label(frm, text="关键字：").grid(row=1, column=0, sticky="nw", pady=(5, 0))
        self.kw_text = tk.Text(frm, height=3, wrap="word", font=("Microsoft YaHei UI", 10),
                               relief="solid", borderwidth=1)
        self.kw_text.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Label(frm, text="多个关键字用换行 / 逗号 / 分号分隔，任一关键字命中即收录；不区分大小写",
                  foreground="#666666").grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 0))

        opt = ttk.Frame(frm)
        opt.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        line1 = ttk.Frame(opt)
        line1.pack(side="top", fill="x")
        ttk.Label(line1, text="并发数：").pack(side="left")
        ttk.Spinbox(line1, from_=1, to=32, width=5,
                    textvariable=self.workers).pack(side="left", padx=(0, 12))
        ttk.Label(line1, text="超时(秒)：").pack(side="left")
        ttk.Spinbox(line1, from_=1, to=120, width=5,
                    textvariable=self.timeout).pack(side="left", padx=(0, 12))
        ttk.Label(line1, text="关注前几页：").pack(side="left")
        ttk.Spinbox(line1, from_=1, to=20, width=4,
                    textvariable=self.max_pages).pack(side="left", padx=(0, 10))
        ttk.Label(line1, text="网页自身有分页时自动跟进前 N 页，每页单独匹配、各占一行（1 = 不翻页）",
                  foreground="#666666").pack(side="left")
        line2 = ttk.Frame(opt)
        line2.pack(side="top", fill="x", pady=(8, 0))
        ttk.Checkbutton(line2, text="忽略大小写",
                        variable=self._ignore_case).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(line2, text="忽略 SSL 证书错误",
                        variable=self._skip_ssl).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(line2, text="仅显示命中", variable=self._only_hits,
                        command=self._refresh_table).pack(side="left")

        line3 = ttk.Frame(opt)
        line3.pack(side="top", fill="x", pady=(8, 0))
        ttk.Label(line3, text="抓取方式：").pack(side="left")
        ttk.Combobox(line3, state="readonly", width=24,
                     textvariable=self.render_mode,
                     values=list(MODE_OPTIONS)).pack(side="left", padx=(0, 12))
        ttk.Label(line3, text="浏览器：").pack(side="left")
        ttk.Combobox(line3, state="readonly", width=16,
                     textvariable=self.browser_channel,
                     values=list(CHANNEL_OPTIONS)).pack(side="left", padx=(0, 12))
        ttk.Label(line3, text="渲染等待(秒)：").pack(side="left")
        ttk.Spinbox(line3, from_=0, to=15, width=4,
                    textvariable=self.render_wait).pack(side="left", padx=(0, 12))
        ttk.Label(line3, text="浏览器并发：").pack(side="left")
        ttk.Spinbox(line3, from_=1, to=8, width=4,
                    textvariable=self.browser_slots).pack(side="left", padx=(0, 12))
        ttk.Button(line3, text="检测浏览器",
                   command=self._detect_browser).pack(side="left")

        line4 = ttk.Frame(opt)
        line4.pack(side="top", fill="x", pady=(4, 0))
        ttk.Label(line4, wraplength=880, justify="left", foreground="#888888",
                  text="浏览器渲染需本机已装 Edge/Chrome；若都没有，请先运行 "
                       "playwright install chromium。智能模式仅在 HTTP 被拦截时"
                       "才启用浏览器，速度更快").pack(side="left")

        line5 = ttk.Frame(opt)
        line5.pack(side="top", fill="x", pady=(8, 0))
        ttk.Checkbutton(line5, text="跟进详情页（在列表页里找具体文章）",
                        variable=self.follow_details).pack(side="left", padx=(0, 12))
        ttk.Label(line5, text="每页最多跟进详情页：").pack(side="left")
        ttk.Spinbox(line5, from_=0, to=50, width=4,
                    textvariable=self.max_details).pack(side="left", padx=(0, 12))
        ttk.Label(line5, wraplength=680, justify="left", foreground="#888888",
                  text="JS 分页点击需『智能』或『全部浏览器渲染』模式；"
                       "仅 HTTP 模式遇到 412 时无法翻页。渲染时真正的并发由"
                       "『浏览器并发』控制（浏览器实例数=该值，调高会占更多内存）；"
                       "渲染等待调到 0.5 秒可进一步提速").pack(side="left")

        bar = ttk.Frame(frm)
        bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.btn_start = ttk.Button(bar, text="开始扫描", command=self._start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(bar, text="停止", command=self._stop_scan, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 18))
        self.btn_xlsx = ttk.Button(bar, text="导出 Excel", command=lambda: self._export("xlsx"), state="disabled")
        self.btn_xlsx.pack(side="left", padx=(0, 6))
        self.btn_csv = ttk.Button(bar, text="导出 CSV", command=lambda: self._export("csv"), state="disabled")
        self.btn_csv.pack(side="left", padx=(0, 18))
        ttk.Button(bar, text="清空结果", command=self._clear).pack(side="left")

        pf = ttk.Frame(frm)
        pf.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        self.progress = ttk.Progressbar(pf, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(pf, textvariable=self.progress_var, width=40).pack(side="left", padx=8)

        tf = ttk.Frame(frm)
        tf.grid(row=6, column=0, columnspan=3, sticky="nsew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=COLS, show="headings", selectmode="extended")
        for c in COLS:
            self.tree.heading(c, text=HEADERS[c])
            self.tree.column(c, width=WIDTHS[c], anchor="w", stretch=(c == "url"))
        vs = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("hit", foreground="#0B6B3A")
        self.tree.tag_configure("fail", foreground="#C0392B")
        self.tree.tag_configure("miss", foreground="#888888")
        self.tree.tag_configure("browser", background="#EAF2FF")
        self.tree.bind("<Double-1>", self._open_url)

        ttk.Label(frm, textvariable=self.status_var, foreground="#444444").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(7, 0))

    # ---------- 事件 ----------
    def _choose(self):
        p = filedialog.askopenfilename(title="选择 Word 文档",
                                       filetypes=[("Word 文档 (*.docx)", "*.docx"), ("所有文件", "*.*")])
        if p:
            self.file_var.set(p)

    def _detect_browser(self):
        """检测本机浏览器渲染通道，探测期间显示等待光标（耗时约 1~3 秒）。"""
        if self._detecting:
            return
        self._detecting = True
        old_cursor = self.cget("cursor")
        try:
            self.config(cursor="watch")
            self.update()
            info = scanner.browser_diagnose() or {}
            available = list(info.get("available", []) or [])
            checked = info.get("checked", {}) or {}
            hint = info.get("hint", "") or ""
            lines = ["可用通道：" + ("、".join(map(str, available)) or "无")]
            for ch, desc in checked.items():
                lines.append(f"  · {ch}：{desc}")
            if hint:
                lines.append("")
                lines.append(hint)
            messagebox.showinfo("浏览器检测", "\n".join(lines), parent=self)
        except Exception as e:
            messagebox.showerror("浏览器检测失败", f"{type(e).__name__}: {e}", parent=self)
        finally:
            try:
                self.config(cursor=old_cursor)
                self.update()
            except tk.TclError:
                pass
            self._detecting = False

    def _start(self):
        path = self.file_var.get().strip().strip('"')
        kws = scanner.parse_keywords(self.kw_text.get("1.0", "end"))
        if not path or not os.path.isfile(path):
            messagebox.showwarning("提示", "请先选择一个存在的 Word 文档（.docx）")
            return
        if not kws:
            messagebox.showwarning("提示", "请至少输入一个关键字")
            return
        if not path.lower().endswith(".docx"):
            messagebox.showwarning("提示", "仅支持 .docx 格式。若为老版 .doc，请先用 Word 另存为 .docx")
            return
        try:
            links, skipped = scanner.extract_links(path)
        except zipfile.BadZipFile:
            messagebox.showerror("读取失败", "文件不是有效的 .docx（可能已损坏或为 .doc 格式）")
            return
        except Exception as e:
            messagebox.showerror("读取失败", f"{type(e).__name__}: {e}")
            return
        if not links:
            messagebox.showinfo("提示", "文档中未找到 http/https 超链接")
            return

        # 在主线程取好参数，避免跨线程访问 Tk 变量；Spinbox 可手输，统一做边界收敛
        try:
            workers = max(1, min(32, int(self.workers.get())))
            timeout = max(1, min(120, int(self.timeout.get())))
            max_pages = max(1, min(20, int(self.max_pages.get())))
        except (tk.TclError, ValueError, TypeError):
            workers, timeout, max_pages = 8, 10, 1
        # 界面文案 -> 后端枚举；非法/空值一律回退到推荐的 auto
        render_mode = MODE_MAP.get(self.render_mode.get(), "auto")
        browser_channel = CHANNEL_MAP.get(self.browser_channel.get(), "auto")
        try:
            render_wait = max(0.0, min(15.0, float(self.render_wait.get())))
        except (tk.TclError, ValueError, TypeError):
            render_wait = 2.0
        try:
            browser_slots = max(1, min(8, int(self.browser_slots.get())))
        except (tk.TclError, ValueError, TypeError):
            browser_slots = 2
        # 详情页跟进参数：非法输入回退 10，收敛到 0..50
        try:
            max_details = max(0, min(50, int(self.max_details.get())))
        except (tk.TclError, ValueError, TypeError):
            max_details = 10
        params = dict(workers=workers, timeout=timeout,
                      ignore_case=self._ignore_case.get(),
                      verify_ssl=not self._skip_ssl.get(), max_pages=max_pages,
                      render_mode=render_mode, browser_channel=browser_channel,
                      render_wait=render_wait, browser_slots=browser_slots,
                      follow_details=self.follow_details.get(),
                      max_details=max_details)
        total_tasks = max(1, len(links) * max_pages)

        self._stop.clear()
        self._running = True
        self._all_rows = []
        self._clear_table()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_xlsx.config(state="disabled")
        self.btn_csv.config(state="disabled")
        self.progress.config(maximum=total_tasks, value=0)
        self.progress_var.set(f"0 / {total_tasks}")
        self.status_var.set(
            f"文档中共 {len(links)} 个起始链接（已过滤 {skipped} 个非网页链接），"
            f"关注前 {max_pages} 页，最多 {total_tasks} 个页面任务，"
            f"抓取方式：{MODE_LABEL.get(render_mode, '智能')}，正在抓取...")
        threading.Thread(target=self._worker, args=(path, kws, params), daemon=True).start()

    def _worker(self, path, kws, params):
        try:
            res = scanner.scan(
                path, kws,
                on_progress=lambda d, t, row: self._q.put(("row", d, t, row)),
                should_stop=self._stop.is_set, **params)
            self._q.put(("done", res))
        except Exception as e:
            self._q.put(("error", f"{type(e).__name__}: {e}"))

    def _drain(self):
        try:
            for _ in range(400):
                msg = self._q.get_nowait()
                kind = msg[0]
                if kind == "row":
                    _, done, total, row = msg
                    # row=None 是「仅更新进度分母」信号（进入详情页阶段时发出）
                    if row is not None:
                        self._all_rows.append(row)
                        self._insert_row(row)
                    # 分母随详情页任务扩展，动态抬高进度条最大值
                    if total > int(self.progress.cget("maximum")):
                        self.progress.config(maximum=total)
                    self.progress.config(value=done)
                    hits = sum(1 for r in self._all_rows if r["status"] == "命中")
                    failed = sum(1 for r in self._all_rows if r["status"] == "失败")
                    detail_cnt = sum(1 for r in self._all_rows
                                     if r.get("level") == "详情页")
                    extra = f" · 含详情页 {detail_cnt} 行" if detail_cnt else ""
                    self.progress_var.set(
                        f"{done} / {total} 页 · 命中 {hits} · 失败 {failed}{extra}")
                elif kind == "done":
                    self._finish(msg[1])
                    # 不能 return：否则跳过末尾的 after() 调度，消息泵永久停止，
                    # 第二次扫描的进度/完成消息无人消费 → 界面假死。break 仅结束
                    # 本批，下一轮 after 会继续调度，支持连续多次扫描。
                    break
                elif kind == "error":
                    self._finish(None, msg[1])
                    break
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, res, error=None):
        self._running = False
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        if error:
            messagebox.showerror("扫描失败", error)
            self.status_var.set("扫描失败：" + error)
            return
        self._all_rows = res["rows"]      # 已按「命中优先」排序
        self._refresh_table()
        pages = res.get("pages", len(res["rows"]))
        self.progress.config(maximum=max(1, pages), value=pages)
        self.progress_var.set(f"{pages} / {pages} 页 · 命中 {res['matched']}")
        miss = pages - res["matched"] - res["failed"]
        tail = "（已手动停止，结果不完整）" if res["stopped"] else ""
        # 浏览器渲染统计为新增键，用 .get() 兼容旧结果
        browser_used = res.get("browser_used", 0) or 0
        browser_failed = res.get("browser_failed", 0) or 0
        mode_txt = MODE_LABEL.get(res.get("render_mode", ""), "HTTP")
        detail_pages = res.get("detail_pages", 0) or 0
        detail_matched = res.get("detail_matched", 0) or 0
        self.status_var.set(
            f"完成：起始链接 {res['total']} 个 | 实际抓取 {pages} 页 | "
            f"命中 {res['matched']} 页 | 未命中 {miss} 页 | 失败 {res['failed']} 页 | "
            f"已过滤非网页链接 {res['skipped']} | 关注前 {res.get('page_limit', 1)} 页 | "
            f"抓取方式 {mode_txt} | 浏览器渲染 {browser_used} 页 / 失败 {browser_failed} 页 | "
            f"详情页 {detail_pages} 行 / 命中 {detail_matched} 行 | "
            "取消勾选「仅显示命中」可查看未命中/失败页；导出的是当前表格中显示的行 " + tail)
        if self._visible_rows():
            self.btn_xlsx.config(state="normal")
            self.btn_csv.config(state="normal")

    def _stop_scan(self):
        self._stop.set()
        self.btn_stop.config(state="disabled")
        self.progress_var.set("正在停止...")

    def _clear(self):
        if self._running:
            messagebox.showinfo("提示", "扫描进行中，请先停止")
            return
        self._all_rows = []
        self._clear_table()
        self.progress.config(value=0)
        self.progress_var.set("就绪")
        self.status_var.set("提示：双击表格任意一行可在浏览器中打开该链接")
        self.btn_xlsx.config(state="disabled")
        self.btn_csv.config(state="disabled")

    def _on_close(self):
        self._stop.set()
        self.destroy()

    # ---------- 表格 ----------
    def _clear_table(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _insert_row(self, row):
        if self._only_hits.get() and row["status"] != "命中":
            return
        pn, pc = row.get("page_no", 1), row.get("page_count", 1)
        page_label = f"{pn}/{pc}" if pn and pc else ""
        tag = {"命中": "hit", "失败": "fail"}.get(row["status"], "miss")
        via = row.get("fetch_via", "HTTP") or "HTTP"
        # 浏览器渲染的页面加淡蓝底标记，不影响命中/失败/未命中的前景色
        tags = (tag, "browser") if via != "HTTP" else (tag,)
        self.tree.insert("", "end", tags=tags, values=(
            row["idx"] + 1, page_label, row.get("level", "列表页"), row["status"],
            "、".join(row["matched"]), row["texts"], row["title"], row["url"],
            via, row["note"]))

    def _visible_rows(self):
        return [r for r in self._all_rows
                if not self._only_hits.get() or r["status"] == "命中"]

    def _refresh_table(self):
        if not hasattr(self, "tree"):
            return
        self._clear_table()
        for row in self._visible_rows():
            self._insert_row(row)

    def _open_url(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        pos = COLS.index("url")
        url = values[pos] if len(values) > pos else ""
        if url.startswith("http"):
            webbrowser.open(url)

    # ---------- 导出 ----------
    def _export(self, kind):
        rows = self._visible_rows()
        if not rows:
            messagebox.showinfo("提示", "当前没有可导出的结果")
            return
        if kind == "xlsx":
            path = filedialog.asksaveasfilename(
                title="导出 Excel", defaultextension=".xlsx", initialfile="命中链接汇总.xlsx",
                filetypes=[("Excel 工作簿 (*.xlsx)", "*.xlsx")])
            fn = scanner.export_xlsx
        else:
            path = filedialog.asksaveasfilename(
                title="导出 CSV", defaultextension=".csv", initialfile="命中链接汇总.csv",
                filetypes=[("CSV 文件 (*.csv)", "*.csv")])
            fn = scanner.export_csv
        if not path:
            return
        try:
            fn(path, rows)
            messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条记录：\n{path}")
        except PermissionError:
            messagebox.showerror("导出失败", "文件被占用，请关闭已打开的同名文件后重试")
        except Exception as e:
            messagebox.showerror("导出失败", f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        # 打包自检：在冻结环境内真实跑一遍 抓取 -> 分页跟进 -> 逐页匹配 -> 导出 -> GUI 构造
        import html as _html
        import os
        import tempfile
        import threading
        import traceback
        import zipfile
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit

        out = os.path.join(tempfile.gettempdir(), "wls_selftest.txt")
        srv = None
        try:
            import requests, bs4, openpyxl  # noqa: F401
            import scanner

            def _check(cond, msg):
                if not cond:
                    raise RuntimeError(msg)

            # 3 页分页站点 + 跨站 / 分页 404 / 分页环 等边界页
            site_pages = {
                "/": ("<html><head><title>自检页面</title></head>"
                      "<body><p>人工智能与大模型</p></body></html>"),
                "/page1": ("<html><head><title>分页一</title></head><body>"
                           "<h1>第一页</h1><p>人工智能正在改变世界</p>"
                           "<div class=\"pagination\"><a href=\"/page1\">1</a>"
                           "<a href=\"/page2\" class=\"next\">下一页 ›</a>"
                           "<a href=\"/page3\">3</a></div></body></html>"),
                "/page2": ("<html><head><title>分页二</title>"
                           "<link rel=\"next\" href=\"/page3\"></head><body>"
                           "<p>大模型技术白皮书</p>"
                           "<a rel=\"next\" href=\"/page3\">Next</a>"
                           "<a href=\"https://example.com/sponsor\">广告</a>"
                           "</body></html>"),
                "/page3": ("<html><head><title>分页三</title></head><body>"
                           "<p>云计算与分布式存储</p>"
                           "<div class=\"pager\"><a href=\"/page1\">下一页</a></div>"
                           "</body></html>"),
                "/offsite": ("<html><head><title>跨站</title>"
                             "<link rel=\"next\" href=\"https://example.com/next\">"
                             "</head><body><p>外站测试</p></body></html>"),
                "/badnext": ("<html><head><title>坏分页</title></head><body>"
                             "<p>人工智能</p><a rel=\"next\" href=\"/missing\">下一页</a>"
                             "</body></html>"),
            }

            class _H(BaseHTTPRequestHandler):
                def do_GET(self):
                    path = urlsplit(self.path).path
                    body_html = self.server.site_pages.get(path)
                    if body_html is None:
                        raw = b"not found"
                        self.send_response(404)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    raw = body_html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def log_message(self, *args):
                    pass

            srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
            srv.site_pages = site_pages
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            # 1) 基础抓取 / 匹配
            text, final, title = scanner.fetch_page(base + "/", 5, True)
            _check("人工智能" in text, "网页正文抓取失败: " + text[:100])
            _check(scanner.find_matches(text, ["人工智能", "不存在的关键字"]) == ["人工智能"],
                   "关键字匹配失败")

            # 2) 关注前 2 页：只抓 2 页，且逐页返回
            got = scanner.fetch_pages(base + "/page1", 2, timeout=5)
            _check(len(got) == 2, f"N=2 应只抓 2 页，实得 {len(got)}")
            _check(got[0].url.endswith("/page1") and got[1].url.endswith("/page2"),
                   "分页跟进 URL 不正确")
            _check("人工智能" in got[0].text and "大模型" in got[1].text,
                   "分页正文抓取不正确")
            _check(all(p.error == "" for p in got), "分页页不应有错误")

            # 3) N=4：/page3 的「下一页」指回 /page1，应被全局去重拦住，只产出 3 页
            got3 = scanner.fetch_pages(base + "/page1", 4, timeout=5)
            _check(len(got3) == 3 and got3[2].url.endswith("/page3"),
                   "分页环应被 URL 去重拦住，不应重复抓第 1 页")

            # 4) 跨站 next 不跟进；分页 404 保留为失败页
            off = scanner.fetch_pages(base + "/offsite", 3, timeout=5)
            _check(len(off) == 1, "跨站 next 不应被跟进")
            bad = scanner.fetch_pages(base + "/badnext", 3, timeout=5)
            _check(len(bad) == 2 and bad[1].error.startswith("HTTP 404"),
                   "分页 404 应保留为失败页，不能静默丢弃")

            # 5) 纯标准库构造最小 docx（冻结环境无 python-docx），跑 scan 全链路
            def _make_min_docx(path, urls):
                w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
                rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                        f'<Relationships xmlns="{pkg}">']
                hls = []
                for i, u in enumerate(urls, 1):
                    rels.append(f'<Relationship Id="rId{i}" Type="{r_ns}/hyperlink" '
                                f'Target="{_html.escape(u, quote=True)}" TargetMode="External"/>')
                    hls.append(f'<w:hyperlink r:id="rId{i}"><w:r><w:t>分页自检{i}</w:t>'
                               f'</w:r></w:hyperlink>')
                rels.append('</Relationships>')
                doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       f'<w:document xmlns:w="{w_ns}" xmlns:r="{r_ns}"><w:body>'
                       f'<w:p>{"".join(hls)}</w:p></w:body></w:document>')
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("word/document.xml", doc)
                    z.writestr("word/_rels/document.xml.rels", "".join(rels))

            docx_path = os.path.join(tempfile.gettempdir(), "wls_selftest.docx")
            _make_min_docx(docx_path, [base + "/page1"])

            res = scanner.scan(docx_path, ["人工智能", "大模型"],
                               workers=2, timeout=5, max_pages=2)
            rows = res["rows"]
            _check(res["total"] == 1 and res["pages"] == 2 and len(rows) == 2,
                   f"scan(N=2) 应产出 2 行，实得 {res}")
            by_page = {r["page_no"]: r for r in rows}
            _check(by_page[1]["url"].endswith("/page1") and by_page[1]["matched"] == ["人工智能"],
                   "第 1 页应独立成行并命中人工智能")
            _check(by_page[2]["url"].endswith("/page2") and by_page[2]["matched"] == ["大模型"],
                   "第 2 页应独立成行并命中大模型")
            _check(all(r["page_count"] == 2 and r["parent_url"] == base + "/page1" for r in rows),
                   "page_count / parent_url 不正确")
            _check(rows[0]["status"] == "命中", "命中行应排在最前")

            # 6) N=1 与旧版一致：每个链接一行
            res1 = scanner.scan(docx_path, ["人工智能"], workers=1, timeout=5, max_pages=1)
            _check(res1["total"] == 1 and res1["pages"] == 1 and len(res1["rows"]) == 1,
                   "N=1 应与旧版一致，每个链接一行")

            # 7) 导出：新增列 + 第 6 列超链接指向该行具体页面 URL
            from openpyxl import load_workbook
            xp = os.path.join(tempfile.gettempdir(), "wls_selftest.xlsx")
            scanner.export_xlsx(xp, rows)
            ws = load_workbook(xp).active
            # 后端 FIELDS 由 11 列扩为 12 列（追加「来源层级」），兼容旧列数
            _check(ws.max_column in (11, 12)
                   and ws.cell(row=1, column=9).value == "页码"
                   and ws.cell(row=1, column=10).value == "起始链接"
                   and ws.cell(row=1, column=12).value == "来源层级", "导出新增列缺失")
            _check(ws.cell(row=2, column=6).hyperlink is not None
                   and ws.cell(row=3, column=6).hyperlink.target.endswith("/page2"),
                   "Excel URL 超链接应指向具体页面")
            _check(os.path.getsize(xp) > 0, "xlsx 导出失败")
            cp = os.path.join(tempfile.gettempdir(), "wls_selftest.csv")
            scanner.export_csv(cp, rows)
            _check(os.path.getsize(cp) > 0, "csv 导出失败")

            # 8) GUI 构造
            app = App()
            app.update()
            ver = app.tk.call("info", "patchlevel")
            app.destroy()
            result = (f"OK | Tk {ver} | 抓取/分页(N=2 抓2页、防环、同源、404保留)/"
                      f"逐页匹配/Excel/CSV/GUI 全链路通过 | {title}")
            # 浏览器诊断仅作可选信息：无浏览器或接口异常都不影响 OK 判定
            try:
                diag = scanner.browser_diagnose() or {}
                avail = list(diag.get("available", []) or [])
                avail_txt = "、".join(map(str, avail)) or "无"
                result += f" | 浏览器诊断：可用 {len(avail)} 个（{avail_txt}）"
            except Exception as diag_exc:
                result += f" | 浏览器诊断不可用（{type(diag_exc).__name__}）"
        except Exception:
            result = "FAIL\n" + traceback.format_exc()
        finally:
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass
        sys.exit(0)

    if "--selftest-browser" in sys.argv:
        # 浏览器渲染自检：本机探测 + data: 页面渲染，不联网、不扫真实站点
        import os
        import tempfile
        import traceback
        from urllib.parse import quote

        out = os.path.join(tempfile.gettempdir(), "wls_selftest_browser.txt")
        try:
            import scanner
            diag = scanner.browser_diagnose() or {}
            available = list(diag.get("available", []) or [])
            checked = diag.get("checked", {}) or {}
            hint = diag.get("hint", "") or ""
            if not available:
                raise RuntimeError("未检测到可用的浏览器渲染通道：" +
                                   (hint or "请先运行 playwright install chromium"))

            from playwright.sync_api import sync_playwright
            html = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                    "<title>浏览器渲染自检</title></head><body>"
                    "<h1>浏览器渲染自检</h1><p>政务公开 测试关键字</p>"
                    "</body></html>")
            data_url = "data:text/html;charset=utf-8," + quote(html)
            channel = str(available[0])
            launch_kw = {"headless": True}
            if channel in ("msedge", "chrome"):
                launch_kw["channel"] = channel
            text = ""
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**launch_kw)
                try:
                    page = browser.new_page()
                    page.goto(data_url, wait_until="load", timeout=15000)
                    text = page.inner_text("body")
                finally:
                    browser.close()
            if "浏览器渲染自检" not in text or "政务公开" not in text:
                raise RuntimeError("浏览器渲染未取到预期正文：" + repr(text[:120]))
            avail_txt = "、".join(map(str, available)) or "无"
            result = (f"OK | 浏览器渲染自检通过 | 通道 {channel} | "
                      f"可用 {len(available)} 个：{avail_txt} | "
                      f"检测明细 {len(checked)} 项 | 正文长度 {len(text)} | {hint}")
        except Exception:
            result = "FAIL\n" + traceback.format_exc()
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass
        sys.exit(0)

    try:
        App().mainloop()
    except Exception:
        # --windowed 打包后无控制台，异常必须弹窗可见
        import traceback
        try:
            messagebox.showerror("程序异常", traceback.format_exc()[-1500:])
        except Exception:
            pass
        raise
