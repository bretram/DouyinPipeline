# 抖音自动管线 · Douyin Auto Pipeline

把抖音分享链接变成「无水印 mp4 + 自动剪辑成片」的**本机**工具。自带网页 UI（手机/电脑同 WiFi 即可访问），可选配合 cpolar 内网穿透在外网提交链接、取回成品。

> ⚠️ 本项目仅用于个人本地归档与剪辑练习。下载行为请遵守抖音平台服务条款与当地法律法规。

## 功能特性

- **网页提交**：粘贴抖音链接 → 后端自动下载（`download_videos.py` 用 Playwright 驱动 Chromium 提取视频直链）。
- **智能剪辑**（`auto_edit.py`，基于 ffmpeg + numpy，零额外付费依赖）：
  - 场景检测（scenedetect）保留精彩片段
  - 节拍检测（音频能量峰值）卡点剪辑
  - 8 种随机特效、10 种 xfade 转场
  - 一键成片 mega 模式（以上随机组合全自动）
- **本地 Web 服务 + cpolar 隧道**，带防睡眠守护，熄屏/锁屏不断服务。
- 历史成品管理与下载，按批次命名、按抖音 ID 去重。

## 目录结构

```
DouyinPipeline/
├── links.txt              # 输入：粘抖音链接，每行一条
├── videos/                # 下载好的视频（不入库）
├── output/                # 剪辑成品 + sidecar json（不入库）
├── logs/                  # 运行 / cpolar 日志（不入库，可能含令牌）
├── cpolar/                # cpolar 二进制（不入库，含账号令牌）
├── metadata.csv           # 视频元数据（不入库，生成物）
├── scripts/
│   ├── linkserver.py      # Web 服务 + 任务编排 + cpolar 隧道
│   ├── download_videos.py # Playwright 下载器
│   ├── auto_edit.py       # ffmpeg / numpy 自动剪辑
│   └── run_hidden.py      # 无窗口启动辅助
├── Start.bat / Stop.bat   # 一键启停（Windows）
├── requirements.txt
└── .gitignore
```

## 环境依赖

- Python 3.10+
- [Playwright](https://playwright.dev/)（需下载 Chromium）
- ffmpeg / ffprobe / [scenedetect](https://www.scenedetect.com/)（命令行工具）
- numpy、requests

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
playwright install chromium
# 将 ffmpeg.exe / ffprobe.exe 放到 .venv\ 下，
# scenedetect.exe 放到 .venv\Scripts\ 下（或用环境变量指定路径，见下）
```

## 配置

所有路径通过环境变量控制，默认值见 `scripts/*.py`：

| 变量 | 说明 | 默认 |
|------|------|------|
| `DOUYIN_PIPELINE_ROOT` | 项目根目录 | `F:\DouyinPipeline` |
| `FFMPEG_BIN` | ffmpeg 可执行文件 | `$ROOT/.venv/ffmpeg.exe` |
| `FFPROBE_BIN` | ffprobe 可执行文件 | `$ROOT/.venv/ffprobe.exe` |
| `SCENEDETECT_BIN` | scenedetect 可执行文件 | `$ROOT/.venv/Scripts/scenedetect.exe` |

> 🔒 安全：本项目**不读取任何 API Key / Cookie 文件**，下载完全依赖 Playwright 模拟浏览器。
> `logs/` 与 `cpolar/` 可能包含账号令牌，已被 `.gitignore` 排除，**切勿手动提交**。
> cpolar 账号令牌由其客户端自行管理，不要写入仓库任何文件。

## 使用

```bash
# 启动（Windows 双击 Start.bat；需管理员以便修改电源计划防睡眠）
python scripts/linkserver.py
```

浏览器打开 `http://localhost:7890`，粘贴抖音链接，选择剪辑模式，点「开始处理」。
手机同 WiFi 可用本机 IP 访问；如需外网访问，启动 cpolar 后页面顶部会显示公网地址。

## 剪辑模式

| 模式 | 说明 |
|------|------|
| `auto` | 随机排序 + 场景检测 + 转场拼接（默认） |
| `split_h` | 左右分屏（2 视频并排 ~5s） |
| `split_v` | 上下分屏（2 视频堆叠 ~5s） |
| `shorts` | 5 秒快剪（每段截 5s 拼接） |
| `mega` | 🤖 一键成片（节拍 + 特效 + 转场全自动） |

命令行直接调用剪辑：

```bash
python scripts/auto_edit.py <batch_id> --mode mega
```

## 说明

- 视频按抖音视频 ID 去重，已下过的自动跳过。
- 一切在本地，无外部上传（除非你主动开启 cpolar 隧道对外暴露）。
- 解析/下载失败会写入 `logs/`，不会中断整轮任务。
