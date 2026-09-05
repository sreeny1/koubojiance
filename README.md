# 口播违禁词检测

> **当前版本：v1.1.0**（版本号见 `app/core/config.py` 的 `APP_VERSION`，界面顶栏同步显示；每次功能/修复发布递增）

批量检测视频口播内容中的违禁词：拖入视频 → 自动转写字幕（本地 AI，GPU 加速）→ 违禁词检索 → 精确到秒的标注与一键定位回放。

> 提示：首次使用若本地无模型，程序会自动从**国内源（ModelScope）**下载（约 3GB，断点续传），全程无需手动操作。

## 功能

- **批量处理**：拖拽 / 系统文件选择 / 手动粘贴路径，三种方式提交，支持整个文件夹扫描
- **本地转写**：faster-whisper（Whisper large-v3），RTX 3060 上 GPU 加速，全程离线、数据不出本机
- **双层违禁词检测**：
  - 归一化精确层：繁简/全角/标点归一，可命中"第 一 名"这类分词规避写法
  - 拼音谐音层："蕞好"→"最好" 等谐音替换也能命中
- **精确定位**：每条命中给出 视频文件 / 时间点 / 命中词 / 完整口播句，点击即在播放器中跳到该秒回放
- **词库管理**：内置 4 分类 88 词种子库（广告法极限词/医疗功效/金融风险/平台违禁），支持增删改、批量导入、启停用；改词库后一键重新检索（无需重新转写）
- **报告导出**：Excel 明细报告、SRT 字幕文件
- **稳定性**：单视频失败不影响整批、失败重试、服务重启后任务自动恢复、设置损坏自动回退

## 快速开始

```
1. 双击 口播违禁词检测.exe（桌面窗口内嵌界面，原生拖放直接读取本地视频，无需复制）
2. 或双击 启动.bat（仅本机调试用：启动服务并自动打开浏览器）
```

前置条件（仅首次）：
```
.venv\Scripts\python.exe tests\download_model.py large-v3
```
从 ModelScope 国内源下载模型（约 3GB，5MB/s+）到 `data\models\local\large-v3\`，之后永久离线。

## 目录结构

```
违禁词检测/
├── 启动.bat                  # 一键启动
├── requirements.txt          # Python 依赖
├── app/
│   ├── main.py               # 启动入口（python app/main.py）
│   ├── core/                 # 业务核心
│   │   ├── config.py         #   路径与设置管理
│   │   ├── database.py       #   SQLite 数据层
│   │   ├── transcriber.py    #   faster-whisper 转写引擎
│   │   ├── detector.py       #   违禁词检测器（双层匹配）
│   │   ├── media.py          #   ffmpeg/时长探测工具
│   │   └── exporter.py       #   Excel 报告导出
│   ├── server/
│   │   ├── api.py            #   FastAPI 路由
│   │   └── tasks.py          #   转写任务队列
│   └── web/                  # 前端（原生 JS 单页）
├── data/                     # 运行时数据（全部收在项目内）
│   ├── app.db                #   SQLite 数据库
│   ├── settings.json         #   设置
│   ├── models/local/         #   whisper 模型
│   ├── subtitles/            #   导出的 SRT
│   ├── media/                #   网页上传的视频落地
│   ├── exports/              #   Excel 报告
│   └── app.log               #   运行日志
├── tests/                    # 测试与工具脚本
└── docs/开发日志.md           # 开发过程记录
```

## 技术要点

- **转写**：faster-whisper + CTranslate2，GPU 不可用时自动降级（float16 → int8_float16 → int8/CPU）
- **存储**：SQLite WAL 模式；字幕与命中结果持久化，换词库只重检索不重转写
- **播放**：FastAPI FileResponse 流式播放（支持 Range），浏览器原生 `<video>` 定位
- **安全**：视频文件仅通过数据库登记的 id 间接访问，无任意路径读取

## 常见问题

- **首次转写很慢 / 卡住**：模型在下载（看 `data/models` 目录增长），或未运行模型下载脚本
- **某视频"未识别到语音内容"**：该视频可能是纯音乐/无人声；也可在设置页换模型重测
- **浏览器无法播放某格式**（如 mkv/avi）：时间点仍准确，可复制时间到本地播放器定位；建议用 mp4
- **GPU 转写显存不足**：设置页把精度改为 int8，或设备改 CPU

## 开发

```
.venv\Scripts\python.exe tests\test_selfcheck.py     # 模块导入自检
.venv\Scripts\python.exe tests\test_detector.py      # 检测器逻辑测试
.venv\Scripts\python.exe tests\make_test_media.py    # 重新生成测试视频
```

开发过程与决策记录见 `docs/开发日志.md`。
