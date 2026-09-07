# 口播违禁词检测

> **当前版本：v1.6.0**（版本号见 `app/core/config.py` 的 `APP_VERSION`，界面顶栏同步显示；每次功能/修复发布递增）

> **绿色便捷版（v1.4.0）**：软件包仅约 150MB——whisper 模型与 NVIDIA CUDA 运行库全部改为**首次启动自动下载**（国内高速源：模型走 ModelScope，CUDA 库走清华/阿里/腾讯 PyPI 镜像；断点续传 + 文件清单 + 大小/SHA256 校验，保证完整不遗漏）。AMD/Intel 机器首次启动只需下载模型，NVIDIA 机器额外自动补 CUDA 库后启用 GPU 加速。

> **日志模式**：软件所有运行日志（服务端/前端/启动器）统一写入软件根目录 `logs\` 文件夹（`app.log` 与 `launcher.log`），设置页可开启「详细日志模式」并查看/打开日志目录，便于排查问题。

批量检测视频口播内容中的违禁词：拖入视频/文件夹 → 本地 AI 转写字幕 → 违禁词检索 → 精确到秒的标注与一键定位回放，并支持**自定义勾选去除违禁词片段**（ffmpeg 精确切割拼接）。

> 提示：首次使用若本地无模型，程序会自动从**多个国内源中选最快的**下载（约 3GB，断点续传）；下载失败时界面提供手动下载引导，全程尽量自动化。

## 功能

- **批量处理**：拖拽 / 系统文件选择 / 手动粘贴路径，三种方式提交；支持**整个文件夹**与**多个文件夹+文件混合拖入**（桌面版原生拖放零复制）
- **本地转写**：faster-whisper（Whisper large-v3）。NVIDIA 显卡 GPU 加速；AMD/Intel/无独显自动降级 CPU int8，全程离线、数据不出本机
- **双层违禁词检测**：
  - 归一化精确层：繁简/全角/标点归一，可命中"第 一 名"这类分词规避写法
  - 拼音谐音层："蕞好"→"最好" 等谐音替换也能命中
- **精确定位**：每条命中给出 视频文件 / 时间点 / 命中词 / 完整口播句，点击即在播放器中跳到该秒回放
- **一键去除违禁词**：在命中列表勾选要去除的词 → ffmpeg 精确剪掉对应音画片段（仅 mp4）→ 自动备份原文件到同目录 → 成品文件名保持不变、参数保持一致
- **词库管理**：内置 4 分类 88 词种子库（广告法极限词/医疗功效/金融风险/平台违禁），支持增删改、批量导入、启停用；改词库后一键重新检索（无需重新转写）
- **报告导出**：Excel 明细报告、SRT 字幕文件
- **界面**：浅色/深色双主题（跟随系统 + 手动切换 + 记忆）、简约动画、新手引导、悬浮提示
- **稳定性**：单视频失败不影响整批、失败重试、服务重启后任务自动恢复、设置损坏自动回退、最低配置门槛检查

## 快速开始

```
1. 双击 口播违禁词检测.exe（桌面窗口内嵌界面，原生拖放直接读取本地视频，无需复制）
2. 或双击 启动.bat（仅本机调试用：启动服务并自动打开浏览器）
```

首次打开会自动检查配置、识别显卡并（若缺模型）自动下载，无需手动操作。

## 目录结构

```
违禁词检测/
├── 启动.bat                  # 一键启动（调试用）
├── 口播违禁词检测.exe          # 桌面启动器（WebView2 内嵌界面 + 原生拖放）
├── requirements.txt          # Python 依赖
├── app/
│   ├── main.py               # 启动入口（python app/main.py）
│   ├── core/                 # 业务核心
│   │   ├── config.py         #   路径与设置管理
│   │   ├── database.py       #   SQLite 数据层
│   │   ├── transcriber.py    #   faster-whisper 转写引擎（GPU 降级链）
│   │   ├── detector.py       #   违禁词检测器（双层匹配）
│   │   ├── cutter.py         #   ffmpeg 精确去词切割
│   │   ├── media.py          #   ffmpeg/时长探测/回收站删除
│   │   ├── exporter.py       #   Excel 报告导出
│   │   ├── model_downloader.py  # 多源竞速下载 + 断点续传
│   │   └── syscheck.py       #   显卡识别 + 最低配置门槛
│   ├── server/
│   │   ├── api.py            #   FastAPI 路由
│   │   ├── tasks.py          #   转写任务队列
│   │   └── cut_tasks.py      #   去词任务队列
│   └── web/                  # 前端（原生 JS 单页 + 双主题 CSS）
├── data/                     # 运行时数据（全部收在项目内，git 忽略）
│   ├── app.db                #   SQLite 数据库
│   ├── settings.json         #   设置
│   ├── models/local/         #   whisper 模型（首启自动下载）
│   ├── subtitles/            #   导出的 SRT
│   ├── media/                #   网页上传的视频落地
│   ├── exports/              #   Excel 报告
│   └── app.log               #   运行日志
├── tests/                    # 测试与工具脚本
├── scripts/                  # 构建/打包脚本
├── tools/Launcher.cs         # C# 桌面启动器源码
├── lib/webview2/             # WebView2 依赖（fetch_webview2 拉取）
├── docs/                     # 开发文档与日志
└── _archive/                 # 归档（历史备份、失效 venv 等，git 忽略）
```

## 技术要点

- **转写**：faster-whisper + CTranslate2；NVIDIA→CUDA，AMD/Intel→CPU int8（ctranslate2 在 Windows 无 ROCm 后端）
- **去词**：ffmpeg `trim/atrim + concat` 音画同步精确裁剪，重编码匹配原编码/分辨率/帧率/采样率
- **存储**：SQLite WAL 模式；字幕与命中结果持久化，换词库只重检索不重转写
- **播放**：FastAPI FileResponse 流式播放（支持 Range），浏览器原生 `<video>` 定位
- **安全**：视频文件仅通过数据库登记的 id 间接访问，无任意路径读取
- **门槛**：内存 ≥ 8GB、磁盘 ≥ 8GB、CPU 需支持 AVX2，不满足则启动时友好提示

## 常见问题

- **首次转写很慢 / 卡住**：模型在自动下载（看 `data/models` 目录增长）；下载失败看界面「手动下载引导」
- **检测到 AMD 显卡却用 CPU**：ctranslate2 在 Windows 不支持 AMD 加速，属正常降级
- **某视频"未识别到语音内容"**：该视频可能是纯音乐/无人声；也可在设置页换模型重测
- **浏览器无法播放某格式**（如 mkv/avi）：时间点仍准确，可复制时间到本地播放器定位；建议用 mp4
- **去词后视频变短了**：勾选的片段（含前后 0.3 秒缓冲）已被剪掉；原文件已备份到同目录

## 开发

```
.venv\Scripts\python.exe tests\test_selfcheck.py     # 模块导入自检
.venv\Scripts\python.exe tests\test_detector.py      # 检测器逻辑测试
.venv\Scripts\python.exe tests\test_split_segments.py # 字幕切分测试
.venv\Scripts\python.exe tests\test_cutter.py        # ffmpeg 去词切割测试
.venv\Scripts\python.exe tests\e2e_test.py           # 端到端（需服务运行 + 模型）
```

打包发布：`powershell -File scripts\pack_full.ps1`（生成绿色免安装包，模型首启自动下载）。

开发文档见 `docs/开发文档.md`，过程记录见 `docs/开发日志.md`。
