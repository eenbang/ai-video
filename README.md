# AI 图文配音演示视频生成系统

输入一个主题或一段中文文案，自动完成：

```text
主题/文案
  -> 恰好 5 页分镜与解说词
  -> 5 张统一风格的 16:9 画面
  -> 逐页中文配音
  -> 按配音时长合成 MP4
  -> 字幕、清单与质量报告
```

## 环境要求

- Windows 10/11
- Python 3.11 或更高版本
- 可访问 Edge TTS 服务（演示模式）
- 使用 AI API 模式时，需要 OpenAI 或兼容服务的 API Key

## 安装

前置条件：

- Python 3.11 或更高版本（安装时勾选“Add python.exe to PATH”）
- 能联网：首次会下载依赖，`imageio-ffmpeg` 首次合成时下载 FFmpeg 二进制
- Edge TTS 需要联网；只想本地验证图片，可把生图后端选为 demo
- 使用 AI 生图需要 SiliconFlow 或 OpenAI 兼容服务的 API Key

在全新电脑上，最省事是运行仓库里的一键引导脚本：

```powershell
.\setup.ps1
```

它会自动创建虚拟环境、安装依赖、把 `.env.example` 复制成 `.env` 并做环境自检。之后编辑 `.env` 填入 `OPENAI_API_KEY`（不填也能用 demo 模式跑通）。

手动安装等价于：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
```

`imageio-ffmpeg` 会随 Python 依赖一起安装，不需要单独配置系统 FFmpeg。

## 在另一台电脑上从零跑通

1. 克隆仓库：`git clone <仓库地址>`。
2. 在仓库目录运行 `.\setup.ps1`（或按上面的手动安装步骤）。
3. 编辑 `.env` 填入 API Key；不填则用 demo 模式，文案走本地模板、生图走本地演示画面，仅 Edge TTS 需要联网。
4. 终端验证：`ai-video --check`，再 `.\run.ps1 "大模型如何改变软件工程" -Mode demo`。
5. 网页版：`.\.venv\Scripts\python.exe -m ai_video.web`，浏览器打开 <http://127.0.0.1:8000>。

ComfyUI 是可选后端，本机未装 ComfyUI 不影响其余功能。如果想配置，继续往下阅读。

## 使用

先检查运行环境：

```powershell
ai-video --check
```

生成一个演示视频：

```powershell
ai-video "大模型如何改变软件工程" --mode demo
```

也可以单独指定图像生成后端：

```powershell
ai-video "大模型如何改变软件工程" --image-backend comfyui
```

也可以直接运行仓库内脚本，不需要手动激活虚拟环境：

```powershell
.\run.ps1 "大模型如何改变软件工程" -Mode demo
```

也可以从 UTF-8 文本文件读取较长的文案：

```powershell
ai-video --input-file .\script.txt --mode demo
```

运行结束后，终端会输出任务 ID、任务目录、最终视频和清单路径。

## Web 界面

除了命令行，项目也提供浏览器界面，覆盖终端的全部功能：新建任务、实时进度、任务列表与详情、视频在线预览与下载、断点续跑、归档/删除、环境自检，以及 `.env` 配置管理。

启动：

```powershell
ai-video-web
```

也可以用模块方式启动：

```powershell
.\.venv\Scripts\python.exe -m ai_video.web
```

启动后用浏览器打开 <http://127.0.0.1:8000>。Web 版与命令行共用同一套流水线和输出目录，生成的视频仍然保存在 `outputs/<task_id>/video.mp4`；上传 `.txt`/`.md` 文件时，内容会读入输入框，等价于命令行的 `--input-file`。

运行模式与生图后端在「新建任务」页直接选择，默认选中 `.env` 里的当前值；设置页不再重复这两个选项。页面左上角和右上角分别提供返回、前进按钮，方便在任务列表和任务详情之间回退。

新建页输入的主题会保留在浏览器里，切换页面再回来不会丢（刷新页面也保留）。多个任务可以同时提交并排队生成，侧边栏会显示“运行中 N 个”和进度；任务列表里运行中/排队的任务可以单独停止。新建页在任务运行期间仍可继续提交新任务，不会被锁住。并发数由 `.env` 的 `WEB_MAX_CONCURRENT_TASKS` 控制，默认 2，生图与合成较吃资源，建议 1~2。

Web 界面采用暗色未来风设计（深空背景、低饱和蓝青点缀、轻微辉光）。任务详情页支持在线编辑分镜、查看每页质检分数，并可单页重做。

## 运行模式

| 模式 | 内容规划 | 图片 | 配音 |
|---|---|---|---|
| `demo` | 本地分镜模板 | 本地 PIL 演示画面 | Edge TTS |
| `api` | OpenAI 兼容接口 | OpenAI 兼容生图接口 | Edge TTS |
| `auto` | 有 Key 时调用 API，失败时使用本地模板 | 由 `IMAGE_BACKEND` 决定 | Edge TTS |

## 断点续跑

生成过程中如果网络或接口中断，可以接着上次的进度继续，不会从头重跑。已经生成的分镜、图片、配音会直接复用，只补没完成的部分。

续跑最近一次任务：

```powershell
ai-video --resume-latest
```

续跑指定任务（用任务 ID 或任务目录都行）：

```powershell
ai-video --resume outputs\20261031_221727_df4949_345719
```

## 归档与删除任务

每次运行都会在 `outputs` 下新建一个任务目录。为了安全，分两步：先归档到 `_archive`，确认不要了再永久删除。

归档（把任务移进 `_archive`，可恢复）：

```powershell
ai-video --archive outputs\<任务目录>   # 归档指定任务
ai-video --archive-latest               # 归档最近一次任务
ai-video --archive-all                  # 归档 outputs 下全部任务
```

永久删除（只能删 `_archive` 里的）：

```powershell
ai-video --delete <任务ID或目录>         # 删除 _archive 内指定任务
ai-video --delete-all                    # 删除 _archive 内全部任务
```

对 `_archive` 外的任务不能直接删除，先 `--archive` 归档，再 `--delete`。

演示画面会明确标注“非 AI 生图”，适合在没有生图额度时验证完整流程。若需要 AI 生图，请配置 `OPENAI_API_KEY`，并按服务商实际情况修改 `OPENAI_BASE_URL` 和模型名称。

## 输出目录

每次运行都会创建 `outputs/<task_id>/`，其中包含：

```text
input.json             原始输入
run_config.json        脱敏后的运行配置
storyboard.json        五页分镜、提示词、解说词和时长
images/page_01.png     五张画面
audio/page_01.wav      五段配音
segments/page_01.mp4   五段中间视频
video.mp4              最终成片
video_no_bgm.mp4       可选：未混背景音乐的中间成片
subtitles.srt          字幕
quality_report.json    页数、图片唯一性、音频时长和 MP4 检查
manifest.json          任务状态、产物路径、服务提供方和费用估算
task_status.json       当前任务进度状态
pipeline.log           运行日志
```

不会将 API Key 写入任务目录或清单。

## 配置

配置项见 `.env.example`。常用配置如下：

- `PIPELINE_MODE`：`auto`、`api` 或 `demo`
- `OPENAI_API_KEY`、`OPENAI_BASE_URL`：兼容 OpenAI 接口的服务地址，可用硅基流动（SiliconFlow）
- `PLANNER_MODEL`：内容规划模型
- `PROMPT_ENHANCE`、`PROMPT_ENHANCE_MODEL`：生图前是否用大模型扩充画面提示词，以及用哪个模型
- `IMAGE_MODEL`、`IMAGE_SIZE`：生图模型与尺寸
- `IMAGE_TEXT`：画面内是否包含中文文字、图表或逻辑图
- `IMAGE_BACKEND`：`demo`、`openai` 或 `comfyui`；图像生成后端可独立切换，默认 `demo`
- `EDGE_TTS_VOICE`、`EDGE_TTS_RATE`：Edge TTS 的音色与语速
- `COMFYUI_BASE_URL`、`COMFYUI_WORKFLOW_FILE`、`COMFYUI_OUTPUT_NODE_ID`：本地 ComfyUI 后端配置
- `VIDEO_WIDTH`、`VIDEO_HEIGHT`、`FPS`
- `PAGE_AUDIO_TAIL_SECONDS`：每页配音之后的留白
- `BURN_SUBTITLES`、`SLIDE_TRANSITIONS`：是否把解说字幕烧录进画面、是否在幻灯片切换时加淡入淡出过渡
- `AI_DISCLOSURE`：是否在画面右下角显示“AI 辅助生成画面”标识
- `BACKGROUND_MUSIC`、`BACKGROUND_MUSIC_VOLUME`
- `EST_*`：用户自行填写的费用估算参数

## 字幕与画面动效

- `BURN_SUBTITLES=true` 时，会把每页解说词烧录到画面底部（在标题栏上方），同时仍会生成独立的 `subtitles.srt`。
- `SLIDE_TRANSITIONS=true` 时，每页开头淡入、结尾淡出，幻灯片切换更平滑；过渡落在配音后的静音尾巴里，不会截断解说。
- 两项默认开启，想恢复原来的静态画面或纯外挂字幕，把它们设为 `false` 即可。

## 使用硅基流动 SiliconFlow

本项目默认配置为硅基流动：内容规划用 `deepseek-ai/DeepSeek-V4-Flash`，生图用 `Qwen/Qwen-Image`，配音用 Edge TTS。

1. 打开 [SiliconFlow API 密钥页](https://cloud.siliconflow.cn/account/ak) 注册并创建 API Key。
2. 把 Key 填入 `.env` 的 `OPENAI_API_KEY`。
3. 生图模型会走 `POST https://api.siliconflow.cn/v1/images/generations`。想换模型，先执行下面的命令查看当前可用列表，再把模型 ID 填进 `IMAGE_MODEL`：

```powershell
$env:SF_KEY = "你的 API Key"
Invoke-RestMethod -Uri "https://api.siliconflow.cn/v1/models?type=image" `
  -Headers @{ Authorization = "Bearer $env:SF_KEY" }
```

4. 运行：

```powershell
.\run.ps1 "Transformer 模型如何理解一句话"
```

## 使用本地 ComfyUI

需要在本地安装并启动 ComfyUI。完整步骤见 [COMFYUI_SETUP.md](COMFYUI_SETUP.md)，并可直接使用 [comfyui/sdxl_workflow_api.json](comfyui/sdxl_workflow_api.json) 作为工作流模板。

## 生图提示词增强

生图前会自动多跑一步提示词扩充：用大模型把每页简短的画面描述改写成具体的英文 image prompt（写清主体、动作、环境、构图、光线与风格），再交给生图模型。这样即使输入只是很短的抽象主题，画面也会更具体、更贴题。

- `PROMPT_ENHANCE=true` 开启（默认），`false` 关闭。
- `PROMPT_ENHANCE_MODEL` 留空时使用 `PLANNER_MODEL`。
- 若扩充调用失败或没配置 API Key，会自动沿用原始提示词，不影响出片。

## 生图质检与自动重画

生图之后会用一个视觉大模型给每页画面打分（0-10），判断是否贴合该页的标题、要点和解说词。分数低于 `QC_MIN_SCORE` 时，自动把改进建议写进提示词重画，最多重画 `QC_MAX_REDRAWS` 次。质检结果写入任务目录的 `quality_checks.json`，并在任务详情页每页卡片上显示分数和重画次数。

- `QC_ENABLED` 开启或关闭，默认开启。
- `QC_MODEL` 质检模型，需支持图片输入；默认用硅基流动实惠款 `Qwen/Qwen3-VL-8B-Instruct`。
- `QC_MIN_SCORE` 通过分数线，默认 6.0。
- `QC_MAX_REDRAWS` 每页最多重画次数，默认 2。

质检在 `IMAGE_BACKEND=openai` 或 `comfyui` 且已配置 API Key 时生效；demo 占位画面不质检。质检调用失败不会中断出片，会保留原图并记一条日志。

## 编辑分镜与单页重做

Web 任务详情页可以编辑每一页的标题、解说词、要点和提示词，保存后点“重做此页”，只重新生成那一页的画面和配音，再重新合成视频，其他页不变。这比整条重跑更快也更省钱。

## 功能完成度自评

MVP 闭环已全部实现：输入主题/文案、恰好 5 页规划、统一风格生图、逐页 Edge TTS 配音、按语音时长控制切页、合成标准 MP4、保存分镜/日志/清单、支持第二主题复测。

可选增强已完成：字幕生成与烧录、背景音乐、页间淡入淡出转场、Web 页面、任务实时进度与排队、失败重试与断点续跑、成本记录、AI 生成标识、生图质检自动重画、在线编辑分镜、单页重做、任务归档与删除、环境自检、在线配置。

尚未实现（可继续扩展）：竖屏 9:16 模板、数字人/口型同步、多语言配音、一键发布到平台。

## 测试

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) "src")
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 合规与限制

- 画面右下角的“AI 辅助生成画面”标识由 `AI_DISCLOSURE` 控制，`.env` 默认关闭；如需合规展示可改为 `true`。
- OpenAI 官方文档已标注 `gpt-4o-mini-tts` 将于 2027 年 1 月 6 日停用；正式长期部署前应将 TTS 适配器迁移到 Realtime API。
- 使用第三方 API、字体、图片和背景音乐时，需要自行确认服务条款与素材授权。
- 费用只按 `.env` 中的 `EST_*` 参数估算，不会自动联网查询实时价格；四类单价分别为 `EST_PLANNER_CNY_PER_CALL`、`EST_IMAGE_CNY_PER_IMAGE`、`EST_TTS_CNY_PER_1K_CHARS`、`EST_QC_CNY_PER_CALL`，填 0 则该项不计费。
- 本项目不包含数字人、口型同步或人工剪辑拼接流程。

