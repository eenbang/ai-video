# ComfyUI 本地生图接入指南

本项目已经内置 `comfyui` 图像后端：程序把分镜提示词提交给本地 ComfyUI，再取回生成的图片参与视频合成。你只需要安装并启动 ComfyUI、放好模型、导出/使用 API 工作流，再改几处 `.env`。

## 1. 前置条件

- NVIDIA 显卡，建议 8GB 显存以上。足够跑 SDXL。
- 磁盘空间预留 20GB 以上（ComfyUI + SDXL 模型约 10GB）。

## 2. 安装并启动 ComfyUI

推荐用官方桌面版，安装最省事：

1. 打开 [https://comfy.org/download](https://comfy.org/download)，下载 Windows 安装程序。
2. 安装后启动 Comfy Desktop，按提示创建一个本地环境（它自动配好 Python 和 PyTorch）。
3. 启动后浏览器会打开 `http://127.0.0.1:8188`，这就是后端地址。

也可以手动安装：`git clone https://github.com/comfyanonymous/ComfyUI`，然后 `python -m pip install -r requirements.txt`，启动命令是 `python main.py`。

## 3. 下载一个 SDXL 模型

把模型文件放到 ComfyUI 的 `models/checkpoints/` 目录。

推荐先用免费的 SDXL 基础模型（约 6.9GB）：

- HuggingFace：[stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
- 下载文件 `sd_xl_base_1.0.safetensors`，放到 `ComfyUI/models/checkpoints/`

放好后，记住这个文件名，第 5 步要用。

## 4. 使用现成工作流模板（推荐）

项目里已经放了一个 API 格式的 SDXL 文生图工作流模板：

- [comfyui/sdxl_workflow_api.json](comfyui/sdxl_workflow_api.json)

这个模板包含标准链路：加载模型 → 正向/负向提示词 → 空 Latent → KSampler → VAE 解码 → 保存图片。

把它里面 `CheckpointLoaderSimple` 节点的 `ckpt_name` 改成你实际下载的模型文件名。例如：

```json
"ckpt_name": "sd_xl_base_1.0.safetensors"
```

模板里已经写好了三个占位符，程序运行时会自动替换：

- `{{positive_prompt}}`：正向提示词（程序根据分镜生成）
- `{{negative_prompt}}`：负向提示词
- `{{seed}}`：每页不同的随机种子

注意：这三个占位符在 JSON 里**不要加引号**，程序替换时会把它们变成合法的 JSON 值。这个模板不要用 ComfyUI 的“加载工作流”打开，它只给程序当模板用。

## 5. 也可以自己导出工作流（自定义时用）

如果你以后想换更复杂的工作流：

1. 在 ComfyUI 界面里搭好你的工作流（至少包含一个 `SaveImage` 节点）。
2. 菜单栏 `File → Export Workflow (API)`，保存成 JSON。
3. 打开这个 JSON，把正向提示词节点的 `text` 值改成 `{{positive_prompt}}`，负向提示词改成 `{{negative_prompt}}`，KSampler 的 `seed` 改成 `{{seed}}`。同样不要加引号。
4. 建议把 `EmptyLatentImage` 的宽高固定成 `1344 x 768`，不要用 `{{width}}`/`{{height}}`（那会变成 1920x1080，SDXL 单次生成容易爆显存）。

## 6. 修改 `.env`

```text
IMAGE_BACKEND=comfyui
COMFYUI_BASE_URL=http://127.0.0.1:8188
COMFYUI_WORKFLOW_FILE=comfyui/sdxl_workflow_api.json
COMFYUI_OUTPUT_NODE_ID=9
```

- `COMFYUI_WORKFLOW_FILE`：指向第 4 步或第 5 步得到的 JSON（建议用绝对路径）。
- `COMFYUI_OUTPUT_NODE_ID`：`SaveImage` 节点的 ID。模板里是 `9`；自己导出的工作流看 JSON 里 `SaveImage` 的键名。

`PIPELINE_MODE` 保持 `auto`，文案和配音仍走硅基流动 + Edge TTS；只有图片改成本地 ComfyUI。

## 7. 运行

先确保 ComfyUI 已经启动并打开在 `127.0.0.1:8188`，然后：

```powershell
.\run.ps1 "Transformer 模型如何理解一句话" -ImageBackend comfyui
```

或者直接改 `.env` 后不加参数：

```powershell
.\run.ps1 "Transformer 模型如何理解一句话"
```

运行完成后看任务目录里的 `manifest.json`，`providers.image` 应该以 `comfyui:` 开头。

## 8. 常见问题

- **连不上 ComfyUI**：确认 ComfyUI 已启动，浏览器能打开 `http://127.0.0.1:8188`。
- **模型文件找不到**：`ckpt_name` 必须和 `ComfyUI/models/checkpoints/` 里的文件名完全一致（含 `.safetensors` 后缀）。
- **报显存不足 / 生成失败**：把 `EmptyLatentImage` 的宽高改成 `1024 x 1024` 或 `1024 x 576`，SDXL 更稳。
- **一直等到超时**：SDXL 在 12GB 显卡上单张约 20～40 秒，5 张总共几分钟；如果太久，检查 ComfyUI 终端是否在正常出图，或把 `COMFYUI_TIMEOUT_SECONDS` 调大。
