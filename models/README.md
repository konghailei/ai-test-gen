# 模型文件目录（models/）

本目录存放 AI-Test-Gen 运行所需的**本地模型文件**，便于离线 / 内网部署。

## 已下载模型

| 模型 | 说明 | 维度 | 体积（约） | 来源 |
|------|------|------|-----------|------|
| `bge-large-zh-v1.5/` | BAAI 中文语义嵌入模型（推荐，中文优化） | 1024 | 1.3 GB | [BAAI/bge-large-zh-v1.5](https://huggingface.co/BAAI/bge-large-zh-v1.5) |

> 本项目只有一个本地模型依赖：嵌入模型。LLM（DeepSeek / 通义千问）走 API，不在此处；
> 也没有本地 rerank / 跨编码器模型。

## 默认行为说明

- 后端 `EMBEDDING_MODEL_TYPE` 默认值为 `chroma_default`：
  使用 ChromaDB 内置 ONNX 模型 `all-MiniLM-L6-v2`（英文优化，自动下载到 `~/.cache/chroma/`）。
- 中文需求场景下**推荐**改用本地 `bge-large-zh-v1.5`（中文优化、效果更好）：
  需将 `EMBEDDING_MODEL_TYPE` 改为 `local`。

## 如何让项目使用本目录的本地模型

编辑 `backend/.env`（没有则从 `backend/.env.example` 复制），修改以下三项：

```dotenv
# 使用本地 sentence-transformers 模型
EMBEDDING_MODEL_TYPE=local

# 指向本目录的模型（绝对路径最稳妥，也可用相对 backend 的 ../models/bge-large-zh-v1.5）
EMBEDDING_MODEL_NAME=D:/git_down/ai-test-gen/models/bge-large-zh-v1.5

# 运行设备：cpu | cuda | mps
EMBEDDING_DEVICE=cpu
```

> 注意：`.env` 中的相对路径以 `backend/` 为工作目录解析，因此用 `../models/bge-large-zh-v1.5`
> 或上面的绝对路径均可。

## Docker 基础镜像内置（推荐，零运行时下载）

基础镜像 `backend/Dockerfile.base` 在构建时直接 `COPY models/bge-large-zh-v1.5` 到
`/app/models/bge-large-zh-v1.5`，并内置 `EMBEDDING_MODEL_NAME=/app/models/bge-large-zh-v1.5`
默认值；`docker-compose.yml` 的后端 `environment` 同样指向该路径。

- 因此 **`docker compose` 部署无需联网下载模型**，只要构建前本目录模型已就绪即可。
- `.dockerignore` 当前**未**排除 `models/`，构建上下文会包含该模型（约 1.3GB），COPY 才能生效。
  切勿把 `models/` 加入 `.dockerignore`，否则镜像内将没有模型、运行时仍会去下载。

## 运行依赖

`local` 模式需要 `sentence-transformers` + `torch`（约数百 MB）。安装方式任选其一：

```bash
# 方式一：项目可选依赖
pip install -e ".[embedding]"

# 方式二：直接安装 requirements 中对应项
pip install sentence-transformers==3.1.1 torch==2.4.0
```

## 重新下载 / 切换源

下载脚本：`scripts/download_model.py`

```bash
# 默认走国内 HuggingFace 镜像（hf-mirror.com），自动落盘到 models/bge-large-zh-v1.5
python scripts/download_model.py

# 可选环境变量
HF_ENDPOINT=https://hf-mirror.com \      # 镜像源（国内加速）
MODEL_REPO=BAAI/bge-large-zh-v1.5 \      # 模型仓库
LOCAL_DIR=D:/git_down/ai-test-gen/models/bge-large-zh-v1.5 \   # 落盘目录
python scripts/download_model.py
```

也可用 ModelScope 源（国内更稳定）：

```bash
pip install modelscope
python -c "from modelscope import snapshot_download; \
snapshot_download('BAAI/bge-large-zh-v1.5', cache_dir='D:/git_down/ai-test-gen/models')"
```

## 代理环境备注（Windows + 本地 HTTP 代理）

若运行环境通过本地 HTTP 代理（如 `HTTPS_PROXY=http://127.0.0.1:10808`）访问外网，
`huggingface_hub` 底层 httpx 在代理上建立 HTTPS 隧道时可能报
`ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`（代理 TLS 兼容性问题），导致下载卡死或中断。
`curl` 走同一代理可正常工作，可改用 `curl` 逐文件下载：

```bash
cd models/bge-large-zh-v1.5
BASE="https://hf-mirror.com/BAAI/bge-large-zh-v1.5/resolve/main"
for f in .gitattributes README.md config.json config_sentence_transformers.json \
         modules.json sentence_bert_config.json special_tokens_map.json \
         tokenizer.json tokenizer_config.json vocab.txt; do
  curl -sL --max-time 120 "$BASE/$f" -o "$f"
done
mkdir -p 1_Pooling
curl -sL --max-time 120 "$BASE/1_Pooling/config.json" -o 1_Pooling/config.json
# pytorch_model.bin 较大（~1.3GB），同样方式下载（或首次用 huggingface_hub 已完成）
# curl -sL --max-time 600 "$BASE/pytorch_model.bin" -o pytorch_model.bin
```
