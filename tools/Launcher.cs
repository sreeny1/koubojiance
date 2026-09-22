using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace LanJinCiLauncher
{
    /// <summary>启动器日志：写入软件根目录 logs\launcher.log（与 Python 服务端日志同目录，便于排查）。</summary>
    static class AppLog
    {
        private static readonly object _lock = new object();
        private static string _path = null;

        public static void Init()
        {
            try
            {
                string dir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "logs");
                Directory.CreateDirectory(dir);
                _path = Path.Combine(dir, "launcher.log");
            }
            catch (Exception) { _path = null; }
        }

        public static void Info(string msg) { Write("INFO", msg); }
        public static void Warn(string msg) { Write("WARN", msg); }
        public static void Error(string msg) { Write("ERROR", msg); }

        private static void Write(string level, string msg)
        {
            if (string.IsNullOrEmpty(_path)) return;
            try
            {
                lock (_lock)
                {
                    File.AppendAllText(_path,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + " [" + level + "] launcher: " +
                        msg + Environment.NewLine, Encoding.UTF8);
                }
            }
            catch (Exception) { }
        }
    }

    static class Program
    {
        // 版本号：保持与 app/core/config.py 的 APP_VERSION 一致（每次发布同步更新）
        private const string Ver = "1.8.4";
        private const string AppTitle = "口播违禁词检测";
        private const string MutexName = "CS_LJJC_LAUNCHER_SINGLETON";
        private const int PortStart = 8765;
        private const int PortTries = 50;

        private static Process _proc;
        private static NotifyIcon _icon;
        private static bool _running;
        private static bool _notified;
        private static MainForm _form;

        // 与 app/web/index.html 拖拽区 / app/server/api.py 保持一致的视频扩展名集合
        private static readonly HashSet<string> _videoExts = new HashSet<string>(
            new[] { ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".flv", ".webm",
                    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg" },
            StringComparer.OrdinalIgnoreCase);

        [STAThread]
        static void Main()
        {
            bool createdNew;
            var mutex = new Mutex(true, MutexName, out createdNew);
            if (!createdNew)
            {
                // 已有服务实例：激活已有窗口后退出
                MainForm.ActivateExisting();
                return;
            }

            AppLog.Init();
            AppDomain.CurrentDomain.UnhandledException += (s, e) =>
                AppLog.Error("AppDomain 未处理异常: " + (e.ExceptionObject as Exception));
            Application.ThreadException += (s, e) =>
                AppLog.Error("UI 线程异常: " + e.Exception);
            AppLog.Info("启动器启动 v" + Ver + " | PID=" + Process.GetCurrentProcess().Id +
                        " | OS=" + Environment.OSVersion + " | .NET=" + Environment.Version +
                        " | exeDir=" + AppDomain.CurrentDomain.BaseDirectory);

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 最低配置门槛：内存 / 磁盘 / AVX2 任一不满足则友好提示并退出
            if (!CheckSystemRequirements())
            {
                AppLog.Warn("最低配置检查未通过，启动器退出");
                return;
            }

            string exeDir = AppDomain.CurrentDomain.BaseDirectory;
            string python = null;
            string rel1 = Path.Combine(exeDir, "python", "python.exe");
            string rel2 = Path.Combine(exeDir, ".venv", "Scripts", "python.exe");
            if (File.Exists(rel1)) python = rel1;
            else if (File.Exists(rel2)) python = rel2;
            string mainPy = Path.Combine(exeDir, "app", "main.py");

            if (python == null || !File.Exists(mainPy))
            {
                MessageBox.Show(
                    "程序文件不完整，请重新解压完整包后再运行。\n\n缺少：python\\python.exe 或 app\\main.py",
                    "口播违禁词检测", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            StartServer(python, mainPy, exeDir);

            _form = new MainForm();
            _form.RestartServer += () => RestartServer(python, mainPy, exeDir);

            _icon = new NotifyIcon();
            _icon.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            _icon.Text = AppTitle + " v" + Ver + " - 正在启动服务…";
            _icon.Visible = true;
            _icon.DoubleClick += (s, e) => _form.ShowFromTray();

            var menu = new ContextMenuStrip();
            var mOpen = new ToolStripMenuItem("打开检测界面");
            mOpen.Click += (s, e) => _form.ShowFromTray();
            var mRestart = new ToolStripMenuItem("重启服务");
            mRestart.Click += (s, e) => _form.RestartFromTray();
            var mAbout = new ToolStripMenuItem("版本信息");
            mAbout.Click += (s, e) => MessageBox.Show(
                AppTitle + "  v" + Ver + "\n\n本机服务版（拖拽/浏览本地素材）\n开发日志见 docs\\开发日志.md",
                AppTitle, MessageBoxButtons.OK, MessageBoxIcon.Information);
            var mExit = new ToolStripMenuItem("退出");
            mExit.Click += (s, e) => { StopServer(); Application.Exit(); };
            menu.Items.Add(mOpen);
            menu.Items.Add(mRestart);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(mAbout);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(mExit);
            _icon.ContextMenuStrip = menu;

            var timer = new System.Windows.Forms.Timer();
            timer.Interval = 1000;
            timer.Tick += (s, e) => WatchServer();
            timer.Start();

            _form.Show();
            Application.Run(_form);
            StopServer();
        }

        private static void StartServer(string python, string mainPy, string exeDir)
        {
            // 启动前清理本项目残留的服务进程（上次异常退出/重复启动可能残留旧实例，
            // 若不清理会与新服务抢端口，造成端口漂移与"启动慢/界面卡启动中"）。
            KillStaleServers(mainPy);
            AppLog.Info("启动服务: python=" + python + " | main.py=" + mainPy + " | workdir=" + exeDir);
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = python,
                    // --no-browser: 由 WebView2 主窗口承载界面，不再自动弹出系统浏览器
                    Arguments = "\"" + mainPy + "\" --no-browser",
                    WorkingDirectory = exeDir,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                // 便携解释器隔离：忽略用户目录 site-packages（避免冲突包），统一 UTF-8（中文路径稳健）
                psi.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                _proc = Process.Start(psi);
                _running = true;
                _notified = false;
                AppLog.Info("服务进程已启动，PID=" + _proc.Id);
                if (_icon != null)
                    _icon.Text = AppTitle + " v" + Ver + " - 服务运行中";
            }
            catch (Exception ex)
            {
                _running = false;
                AppLog.Error("启动服务失败: " + ex);
                MessageBox.Show("启动服务失败：" + ex.Message,
                    AppTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        private static void RestartServer(string python, string mainPy, string exeDir)
        {
            StopServer();
            StartServer(python, mainPy, exeDir);
        }

        private static void KillStaleServers(string mainPy)
        {
            try
            {
                // 本项目 main.py 服务进程的典型命令行特征：
                //  ...\python.exe "..."\app\main.py [--no-browser]
                string marker = "app\\main.py";
                foreach (var p in Process.GetProcessesByName("python"))
                {
                    try
                    {
                        string cl = "";
                        using (var mo = new System.Management.ManagementObjectSearcher(
                            "SELECT CommandLine FROM Win32_Process WHERE ProcessId=" + p.Id))
                        {
                            foreach (var o in mo.Get())
                            {
                                cl = (string)o["CommandLine"] ?? "";
                                break;
                            }
                        }
                        if (cl.IndexOf(marker, StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            try { p.Kill(); } catch (Exception) { }
                            AppLog.Warn("已清理残留服务进程 PID=" + p.Id);
                        }
                    }
                    catch (Exception) { /* 忽略无权限/已退出进程 */ }
                }
            }
            catch (Exception) { }
        }

        private static void WatchServer()
        {
            if (!_running || _proc == null) return;
            if (_proc.HasExited)
            {
                _running = false;
                AppLog.Warn("服务进程已退出（PID=" + _proc.Id + "，退出码 " + _proc.ExitCode + "）");
                if (!_notified)
                {
                    _notified = true;
                    _icon.ShowBalloonTip(5000, AppTitle,
                        "服务已停止。\n可通过托盘图标右键菜单「重启服务」。", ToolTipIcon.Info);
                    _icon.Text = AppTitle + " v" + Ver + " - 服务已停止";
                }
            }
        }

        private static void StopServer()
        {
            _running = false;
            AppLog.Info("停止服务（PID=" + (_proc != null ? _proc.Id.ToString() : "?") + "）");
            if (_proc != null && !_proc.HasExited)
            {
                // 1) 通知服务优雅退出（停 worker、关数据库、进程自行结束）
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(
                        "http://127.0.0.1:" + FindServerPortOr(8765) + "/api/shutdown");
                    req.Method = "POST";
                    req.Timeout = 1500;
                    req.ContentLength = 0;
                    using (var resp = (HttpWebResponse)req.GetResponse()) { }
                }
                catch (Exception) { }

                // 2) 等待优雅退出完成
                if (!_proc.WaitForExit(2000))
                {
                    // 3) 兜底：进程树强杀，确保 ffmpeg 等子进程零残留
                    AppLog.Warn("服务未在 2s 内优雅退出，执行进程树强杀（taskkill /T /F）");
                    try
                    {
                        var psi = new ProcessStartInfo(
                            "taskkill", "/PID " + _proc.Id + " /T /F")
                        {
                            UseShellExecute = false,
                            CreateNoWindow = true,
                            WindowStyle = ProcessWindowStyle.Hidden
                        };
                        using (Process.Start(psi)) { }
                        _proc.WaitForExit(3000);
                    }
                    catch (Exception) { }
                }
                if (!_proc.HasExited)
                {
                    try { _proc.Kill(); } catch (Exception) { }
                }
            }
            _proc = null;
        }

        private static int FindServerPortOr(int fallback)
        {
            int p = FindServerPort();
            return p > 0 ? p : fallback;
        }

        internal static NotifyIcon TrayIcon
        {
            get { return _icon; }
        }

        internal static int FindServerPort()
        {
            // 仅探测 2 个最新端口：8765（主端口）与 8766（8765 被占用时的第二候选）。
            // 每次超时 300ms，避免残留进程占用旧端口时被阻塞数秒。
            for (int p = PortStart; p < PortStart + 2; p++)
            {
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(
                        "http://127.0.0.1:" + p + "/api/status");
                    req.Timeout = 300;
                    using (var resp = (HttpWebResponse)req.GetResponse())
                    {
                        return p;
                    }
                }
                catch (Exception) { continue; }
            }
            return 0;
        }

        internal static bool IsVideoFile(string path)
        {
            try { return _videoExts.Contains(Path.GetExtension(path)); }
            catch (Exception) { return false; }
        }

        internal static string PostScanPaths(IList<string> paths)
        {
            int port = FindServerPort();
            if (port <= 0) return "服务未就绪或已停止，请重新打开软件或从托盘右键「重启服务」。";
            try
            {
                var payload = new StringBuilder();
                payload.Append("{\"paths\":[");
                for (int i = 0; i < paths.Count; i++)
                {
                    if (i > 0) payload.Append(',');
                    payload.Append('"').Append(EscJson(paths[i])).Append('"');
                }
                payload.Append("]}");
                var req = (HttpWebRequest)WebRequest.Create(
                    "http://127.0.0.1:" + port + "/api/scan");
                req.Method = "POST";
                req.ContentType = "application/json";
                req.Timeout = 20000;
                byte[] body = Encoding.UTF8.GetBytes(payload.ToString());
                req.ContentLength = body.Length;
                using (var s = req.GetRequestStream()) s.Write(body, 0, body.Length);
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    if (resp.StatusCode == HttpStatusCode.OK) return "";
                    // 读取服务端返回的 detail（如"模型尚未下载完成"），给用户明确原因
                    string detail = "";
                    try
                    {
                        using (var reader = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                        {
                            string json = reader.ReadToEnd();
                            // 简单解析 {"detail": "..."}
                            int i = json.IndexOf("\"detail\"");
                            if (i >= 0)
                            {
                                int q1 = json.IndexOf('"', i + 8);
                                if (q1 >= 0)
                                {
                                    int q2 = json.IndexOf('"', q1 + 1);
                                    if (q2 > q1) detail = json.Substring(q1 + 1, q2 - q1 - 1);
                                }
                            }
                        }
                    }
                    catch (Exception) { }
                    return string.IsNullOrEmpty(detail) ? "提交失败（服务返回错误），请稍后重试。" : detail;
                }
            }
            catch (Exception) { return "提交失败：网络异常或服务不可用，请稍后重试。"; }
        }

        private static string EscJson(string s)
        {
            var sb = new StringBuilder();
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default: sb.Append(c); break;
                }
            }
            return sb.ToString();
        }

        // ------------------------- 最低配置门槛 -------------------------
        [StructLayout(LayoutKind.Sequential)]
        private struct MEMORYSTATUSEX
        {
            public uint dwLength;
            public uint dwMemoryLoad;
            public ulong ullTotalPhys;
            public ulong ullAvailPhys;
            public ulong ullTotalPageFile;
            public ulong ullAvailPageFile;
            public ulong ullTotalVirtual;
            public ulong ullAvailVirtual;
            public ulong ullAvailExtendedVirtual;
        }

        [DllImport("kernel32.dll")]
        private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);

        [DllImport("kernel32.dll")]
        private static extern bool IsProcessorFeaturePresent(uint processorFeature);

        private static bool CheckSystemRequirements()
        {
            var problems = new List<string>();
            try
            {
                var m = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf(typeof(MEMORYSTATUSEX)) };
                if (GlobalMemoryStatusEx(ref m))
                {
                    double ramGB = m.ullTotalPhys / (1024.0 * 1024 * 1024);
                    if (ramGB < 8.0) problems.Add(string.Format("物理内存不足（{0:F1}GB，最低 8GB）", ramGB));
                }
            }
            catch (Exception) { }

            try
            {
                var drive = new DriveInfo(AppDomain.CurrentDomain.BaseDirectory);
                double freeGB = drive.AvailableFreeSpace / (1024.0 * 1024 * 1024);
                if (freeGB < 8.0) problems.Add(string.Format("磁盘剩余空间不足（{0:F1}GB，最低 8GB）", freeGB));
            }
            catch (Exception) { }

            try
            {
                if (!IsProcessorFeaturePresent(40)) problems.Add("CPU 不支持 AVX2 指令集（本地转写引擎无法运行）");
            }
            catch (Exception) { }

            if (problems.Count > 0)
            {
                MessageBox.Show(
                    "当前电脑配置过低，无法使用本软件：\n\n" + string.Join("\n", problems) +
                    "\n\n建议升级硬件后再运行。",
                    AppTitle, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return false;
            }
            return true;
        }
    }

    /// <summary>WebView2 主窗口：内嵌检测界面 + 原生文件拖放（真实路径直达 /api/scan，零复制）。</summary>
    class MainForm : Form
    {
        private WebView2 _web;
        private System.Windows.Forms.Label _loading;
        private System.Windows.Forms.Panel _dropOverlay;
        private System.Windows.Forms.Panel _dropCard;
        private System.Windows.Forms.Timer _overlayGuard;
        private System.Windows.Forms.Timer _retry;
        private readonly object _navLock = new object();
        private int _lastPort;
        private bool _hasCore;
        // 拖放覆盖层主题（跟随网页设置/系统深色）
        private bool _overlayDark;
        private Color _ovBackdrop, _ovCard, _ovAccent, _ovInk, _ovInk2, _ovInk3;
        private System.Windows.Forms.Label _ovTitle, _ovSub, _ovFmt;

        public event Action RestartServer;

        /// <summary>已有实例时：找到并激活主窗口（跨进程唤起）。</summary>
        internal static void ActivateExisting()
        {
            try
            {
                var procs = System.Diagnostics.Process.GetProcessesByName(
                    System.IO.Path.GetFileNameWithoutExtension(
                        Application.ExecutablePath));
                if (procs.Length > 0)
                {
                    var h = procs[0].MainWindowHandle;
                    if (h == IntPtr.Zero)
                    {
                        h = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle;
                    }
                    if (h != IntPtr.Zero)
                    {
                        Win32.ShowWindow(h, 9 /*SW_RESTORE*/);
                        Win32.SetForegroundWindow(h);
                    }
                }
            }
            catch (Exception) { }
        }

        public MainForm()
        {
            Text = Program_AppTitle() + " v" + Program_Ver();
            Size = new Size(1280, 820);
            MinimumSize = new Size(960, 640);
            StartPosition = FormStartPosition.CenterScreen;
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

            // 拖放覆盖层初始主题：先按设置/系统解析，后续由前端 theme 消息实时更新
            _overlayDark = ResolveInitialDark();
            ResolveOverlayColors();

            // 全窗拖放覆盖层：平时完全隐藏（界面即纯网页，无任何额外 UI）。
            // 当文件拖入网页时，前端 JS 通过 postMessage 通知显示本层，
            // 由它（纯 WinForms，AllowDrop 可用）接管拖放并取真实路径 → /api/scan 零复制。
            _dropOverlay = new Panel
            {
                Dock = DockStyle.Fill,
                BackColor = _ovBackdrop,
                Visible = false,
                AllowDrop = true
            };

            // 居中拖放卡片（圆角 + 虚线边框 + 图标），替代原朴素两行文字
            _dropCard = new Panel
            {
                Size = new Size(540, 300),
                BackColor = _ovCard,
                AllowDrop = true
            };
            _dropCard.Paint += (s, pe) =>
            {
                pe.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (var pen = new Pen(_ovAccent, 2f))
                {
                    pen.DashStyle = DashStyle.Dash;
                    var r = new Rectangle(2, 2, _dropCard.Width - 5, _dropCard.Height - 5);
                    using (var path = RoundedRect(r, 20))
                        pe.Graphics.DrawPath(pen, path);
                }
            };
            SetRoundedRegion(_dropCard, 20);

            var ovIcon = new PictureBox
            {
                Size = new Size(56, 56),
                Location = new Point((_dropCard.Width - 56) / 2, 36),
                SizeMode = PictureBoxSizeMode.Zoom,
                BackColor = Color.Transparent,
                AllowDrop = true
            };
            try { ovIcon.Image = Icon.ExtractAssociatedIcon(Application.ExecutablePath).ToBitmap(); }
            catch (Exception) { /* 图标缺失时忽略 */ }

            _ovTitle = new Label
            {
                Text = "松开鼠标，开始检测",
                Bounds = new Rectangle(0, 108, _dropCard.Width, 42),
                TextAlign = ContentAlignment.MiddleCenter,
                Font = new Font("Microsoft YaHei UI", 20F, FontStyle.Bold),
                ForeColor = _ovInk,
                AllowDrop = true
            };
            _ovSub = new Label
            {
                Text = "可拖入多个视频 / 音频文件，或整个文件夹（自动扫描其中媒体）",
                Bounds = new Rectangle(24, 160, _dropCard.Width - 48, 32),
                TextAlign = ContentAlignment.MiddleCenter,
                Font = new Font("Microsoft YaHei UI", 12F),
                ForeColor = _ovInk2,
                AllowDrop = true
            };
            _ovFmt = new Label
            {
                Text = "mp4 · mov · mkv · avi · mp3 · wav · m4a 等 · 零复制 · 支持整个文件夹",
                Bounds = new Rectangle(24, 198, _dropCard.Width - 48, 26),
                TextAlign = ContentAlignment.MiddleCenter,
                Font = new Font("Microsoft YaHei UI", 10F),
                ForeColor = _ovInk3,
                AllowDrop = true
            };

            _dropCard.Controls.Add(ovIcon);
            _dropCard.Controls.Add(_ovTitle);
            _dropCard.Controls.Add(_ovSub);
            _dropCard.Controls.Add(_ovFmt);
            _dropOverlay.Controls.Add(_dropCard);
            _dropOverlay.Resize += (s, e) => CenterDropCard();

            // 所有子控件都挂同一组拖放事件（OLE 目标按 HWND 查找，子控件也要注册）
            foreach (var c in new Control[] { _dropOverlay, _dropCard, ovIcon, _ovTitle, _ovSub, _ovFmt })
            {
                c.DragEnter += OnOverlayDragEnter;
                c.DragDrop += OnOverlayDragDrop;
            }
            // 拖出覆盖层（离开窗口）时自动隐藏（部分系统不发此通知，靠下方守护定时器兜底）
            _dropOverlay.DragLeave += (s, e) => HideDropOverlay();

            // 守护定时器：覆盖层显示期间 150ms 轮询，发现"鼠标按键已松开"
            //（拖拽取消/在窗外释放）或"光标已移出窗口"即隐藏恢复网页。
            // 必要性：覆盖层是拖拽中途才显示的，OLE 拖放管理器不会补发 DragEnter，
            // 因此拖出窗口时 DragLeave 也不保证触发，必须主动检测。
            _overlayGuard = new System.Windows.Forms.Timer { Interval = 150 };
            _overlayGuard.Tick += OverlayGuardTick;

            _loading = new Label
            {
                Text = "正在启动…",
                Dock = DockStyle.Fill,
                TextAlign = ContentAlignment.MiddleCenter,
                Font = new Font("Microsoft YaHei UI", 16F),
                ForeColor = Color.FromArgb(120, 120, 120)
            };

            _web = new WebView2 { Dock = DockStyle.Fill };
            // 独立数据目录：避免与系统浏览器/其它 WebView2 应用共用默认目录
            // 导致首次初始化慢或锁竞争；目录收在项目内（"文件不出项目"约束）。
            try
            {
                var userData = Path.Combine(
                    AppDomain.CurrentDomain.BaseDirectory, "data", "webview2-data");
                _web.CreationProperties = new CoreWebView2CreationProperties
                {
                    UserDataFolder = userData
                };
            }
            catch (Exception) { /* 默认目录兜底 */ }
            Controls.Add(_web);
            Controls.Add(_loading);
            Controls.Add(_dropOverlay);
            _dropOverlay.BringToFront();
            _loading.BringToFront();
            _web.Visible = false;

            Shown += async (s, e) => await InitWebView();
        }

        // 类型名冲突规避：从 Program 暴露的常量更清晰
        private static string Program_AppTitle() { return "口播违禁词检测"; }
        private static string Program_Ver() { return "1.8.4"; }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (e.CloseReason == CloseReason.UserClosing)
            {
                // 点 X：最小化到托盘，服务继续后台运行
                e.Cancel = true;
                Hide();
                var icon = Program.TrayIcon;
                if (icon != null)
                    icon.ShowBalloonTip(2000, "口播违禁词检测", "程序仍在托盘运行，可右键托盘图标退出。", ToolTipIcon.Info);
                return;
            }
            base.OnFormClosing(e);
        }

        private async Task InitWebView()
        {
            // EnsureCoreWebView2Async 首次运行可能耗时数秒（创建数据目录、启动内核），
            // 更新占位文案，避免用户误以为卡死。
            _loading.Text = "正在启动界面引擎…";
            _loading.ForeColor = Color.FromArgb(120, 120, 120);
            try
            {
                await _web.EnsureCoreWebView2Async();
                _hasCore = true;
                AppLog.Info("WebView2 核心初始化成功");
                BindCoreHandlers();
                TryNavigate();
            }
            catch (Exception ex)
            {
                AppLog.Error("WebView2 初始化失败: " + ex);
                _loading.Text = "WebView2 初始化失败：" + ex.Message;
                _loading.ForeColor = Color.Firebrick;
            }
        }

        private void BindCoreHandlers()
        {
            var cw = _web.CoreWebView2;
            // 新窗口请求（如 SRT 预览）统一在当前窗口导航，避免弹出多余窗口
            cw.NewWindowRequested += (s, e) =>
            {
                e.Handled = true;
                try { cw.Navigate(e.Uri); } catch (Exception) { }
            };
            // 前端通知：文件拖入网页 → 立即显示全窗原生覆盖层接管拖放；
            // theme-dark / theme-light → 让覆盖层跟随网页主题切换
            cw.WebMessageReceived += (s, e) =>
            {
                try
                {
                    string msg = e.TryGetWebMessageAsString();
                    if (msg == "native-drag-enter")
                        ShowDropOverlay();
                    else if (msg == "theme-dark") { _overlayDark = true; ApplyOverlayTheme(); }
                    else if (msg == "theme-light") { _overlayDark = false; ApplyOverlayTheme(); }
                }
                catch (Exception) { /* JSON 消息走 TryGet 会抛异常，忽略 */ }
            };
        }

        private void TryNavigate()
        {
            if (!_hasCore) return;
            lock (_navLock)
            {
                int port = Program.FindServerPort();
                if (port <= 0)
                {
                    if (_retry == null)
                    {
                        _retry = new System.Windows.Forms.Timer();
                        _retry.Interval = 1500;
                        _retry.Tick += (s, e) => TryNavigate();
                        _retry.Start();
                    }
                    _loading.Text = "服务启动中…";
                    _loading.BringToFront();
                    _web.Visible = false;
                    return;
                }
                if (_retry != null) { _retry.Stop(); _retry.Dispose(); _retry = null; }
                string url = "http://127.0.0.1:" + port;
                if (_lastPort != port || _web.Source == null)
                {
                    _lastPort = port;
                    AppLog.Info("导航到服务地址: " + url);
                    try { _web.CoreWebView2.Navigate(url); } catch (Exception ex) { AppLog.Error("导航失败: " + ex); }
                }
                _web.Visible = true;
                _loading.SendToBack();
            }
        }

        /// <summary>托盘「打开检测界面」：显示窗口并确保页面已加载。</summary>
        public void ShowFromTray()
        {
            Show();
            if (WindowState == FormWindowState.Minimized)
                WindowState = FormWindowState.Normal;
            Activate();
            if (_hasCore) TryNavigate();
        }

        /// <summary>托盘「重启服务」：重启后重新导航。</summary>
        public void RestartFromTray()
        {
            _lastPort = 0;
            if (RestartServer != null) RestartServer();
            if (_hasCore) TryNavigate();
        }

        // ------------------------- 原生拖放：真实路径 /api/scan -------------------------
        /// <summary>显示全窗拖放覆盖层（由前端 native-drag-enter 通知触发）。</summary>
        private void ShowDropOverlay()
        {
            if (InvokeRequired) { BeginInvoke((Action)ShowDropOverlay); return; }
            ApplyOverlayTheme();  // 显示前确保颜色与当前主题一致
            CenterDropCard();
            _dropOverlay.Visible = true;
            _dropOverlay.BringToFront();
            _overlayGuard.Start();
        }

        private void CenterDropCard()
        {
            if (_dropCard == null || _dropOverlay == null) return;
            _dropCard.Location = new Point(
                Math.Max(0, (_dropOverlay.ClientSize.Width - _dropCard.Width) / 2),
                Math.Max(0, (_dropOverlay.ClientSize.Height - _dropCard.Height) / 2));
        }

        private static GraphicsPath RoundedRect(Rectangle r, int radius)
        {
            var path = new GraphicsPath();
            int d = radius * 2;
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        private static void SetRoundedRegion(Control c, int radius)
        {
            using (var path = RoundedRect(new Rectangle(0, 0, c.Width, c.Height), radius))
                c.Region = new Region(path);
        }

        // ------------------------- 拖放覆盖层主题（浅色/深色） -------------------------
        private void ResolveOverlayColors()
        {
            if (_overlayDark)
            {
                _ovBackdrop = Color.FromArgb(20, 22, 28);
                _ovCard = Color.FromArgb(30, 33, 41);
                _ovAccent = Color.FromArgb(106, 131, 255);
                _ovInk = Color.FromArgb(231, 234, 242);
                _ovInk2 = Color.FromArgb(166, 173, 189);
                _ovInk3 = Color.FromArgb(109, 116, 132);
            }
            else
            {
                _ovBackdrop = Color.FromArgb(232, 237, 248);
                _ovCard = Color.White;
                _ovAccent = Color.FromArgb(79, 110, 247);
                _ovInk = Color.FromArgb(28, 35, 51);
                _ovInk2 = Color.FromArgb(91, 100, 120);
                _ovInk3 = Color.FromArgb(150, 158, 176);
            }
        }

        private void ApplyOverlayTheme()
        {
            ResolveOverlayColors();
            if (InvokeRequired) { BeginInvoke((Action)ApplyOverlayTheme); return; }
            if (_dropOverlay != null) _dropOverlay.BackColor = _ovBackdrop;
            if (_dropCard != null) { _dropCard.BackColor = _ovCard; _dropCard.Invalidate(); }
            if (_ovTitle != null) _ovTitle.ForeColor = _ovInk;
            if (_ovSub != null) _ovSub.ForeColor = _ovInk2;
            if (_ovFmt != null) _ovFmt.ForeColor = _ovInk3;
        }

        private bool ResolveInitialDark()
        {
            try
            {
                var settingsPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "data", "settings.json");
                if (File.Exists(settingsPath))
                {
                    var m = System.Text.RegularExpressions.Regex.Match(
                        File.ReadAllText(settingsPath, Encoding.UTF8), "\"theme\"\\s*:\\s*\"([a-z]+)\"");
                    if (m.Success)
                    {
                        var t = m.Groups[1].Value;
                        if (t == "dark") return true;
                        if (t == "light") return false;
                    }
                }
            }
            catch (Exception) { }
            return IsSystemDark();
        }

        private static bool IsSystemDark()
        {
            try
            {
                using (var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                    @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
                {
                    if (k == null) return false;
                    object val = k.GetValue("AppsUseLightTheme");
                    if (val is int) return ((int)val) == 0;
                }
                return false;
            }
            catch (Exception) { return false; }
        }

        private void HideDropOverlay()
        {
            if (InvokeRequired) { BeginInvoke((Action)HideDropOverlay); return; }
            _dropOverlay.Visible = false;
            _overlayGuard.Stop();
        }

        /// <summary>覆盖层守护：拖拽取消（按键松开/光标出窗）时恢复网页显示。</summary>
        private void OverlayGuardTick(object sender, EventArgs e)
        {
            try
            {
                if (!_dropOverlay.Visible) { _overlayGuard.Stop(); return; }
                if (!IsDragStillActive()) HideDropOverlay();
            }
            catch (Exception) { _overlayGuard.Stop(); }
        }

        /// <summary>
        /// 拖拽是否仍在进行：左键仍被物理按下 且 光标仍在窗口内。
        /// 注意：OLE 拖拽期间 Control.MouseButtons 不可靠（可能误报 None），
        /// 必须用 GetAsyncKeyState 读物理按键状态，否则覆盖层会被误隐藏从而闪烁。
        /// </summary>
        private bool IsDragStillActive()
        {
            bool btnDown;
            try
            {
                btnDown = (Win32.GetAsyncKeyState(0x01) & 0x8000) != 0;
            }
            catch (Exception)
            {
                btnDown = (Control.MouseButtons & MouseButtons.Left) != 0;
            }
            if (!btnDown) return false;
            // 光标是否仍在窗口内（把屏幕坐标转成客户端坐标再判断）
            try
            {
                return ClientRectangle.Contains(PointToClient(Cursor.Position));
            }
            catch (Exception)
            {
                return true; // 坐标转换失败时保守不隐藏
            }
        }

        private void OnOverlayDragEnter(object sender, DragEventArgs e)
        {
            if (e.Data.GetDataPresent(DataFormats.FileDrop))
                e.Effect = DragDropEffects.Copy;
            else
                e.Effect = DragDropEffects.None;
        }

        private void OnOverlayDragDrop(object sender, DragEventArgs e)
        {
            // 无论成败都先隐藏覆盖层，恢复网页原状
            HideDropOverlay();
            string[] paths = null;
            if (e.Data.GetDataPresent(DataFormats.FileDrop))
                paths = e.Data.GetData(DataFormats.FileDrop) as string[];
            if (paths == null || paths.Length == 0)
                return;

            // 支持"多个文件 + 多个文件夹"混合拖入：文件过滤媒体扩展名，文件夹整体交给后端递归扫描
            var media = new List<string>();
            int folderCount = 0;
            foreach (var p in paths)
            {
                try
                {
                    if (Directory.Exists(p)) { media.Add(p); folderCount++; }
                    else if (Program.IsVideoFile(p)) media.Add(p);
                }
                catch (Exception) { /* 忽略无法访问的路径 */ }
            }
            if (media.Count == 0)
            {
                MessageBox.Show("拖入的内容不支持。\n支持：mp4 / mov / mkv / avi / mp3 / wav / m4a 等媒体文件，或包含这些媒体的整个文件夹。",
                    Program_AppTitle(), MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            string err = Program.PostScanPaths(media);
            if (err.Length == 0)
            {
                AppLog.Info("原生拖放提交成功: " + media.Count + " 个路径（含 " + folderCount + " 个文件夹）");
                NotifyScanSubmitted(media.Count);
            }
            else
            {
                AppLog.Warn("原生拖放提交失败: " + err);
                MessageBox.Show(err, Program_AppTitle(),
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private void NotifyScanSubmitted(int count)
        {
            try
            {
                if (_hasCore)
                    _web.CoreWebView2.PostWebMessageAsJson(
                        "{\"type\":\"scan-submitted\",\"count\":" + count + "}");
            }
            catch (Exception) { }
        }
    }

    /// <summary>Win32 窗口激活辅助（单实例唤起）。</summary>
    static class Win32
    {
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        internal static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [System.Runtime.InteropServices.DllImport("user32.dll")]
        internal static extern bool SetForegroundWindow(IntPtr hWnd);

        // VK_LBUTTON=0x01；高位置位表示该键当前被物理按下
        [System.Runtime.InteropServices.DllImport("user32.dll")]
        internal static extern short GetAsyncKeyState(int vKey);
    }
}