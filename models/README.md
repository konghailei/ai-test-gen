# 模型文件目录（models/）

本目录存放 AI-Test-Gen 运行所需的**本地模型文件**，便于离线 / 内网部署。

## 已下载模型

| 模型 | 说明 | 维度 | 体积（约） | 来源 |
|------|------|------|-----------|------|
| `bge-large-zh-v1.5/` | BAAI 中文语义嵌入模型（推荐，中文优化） | 1024 | 1.3 GB | [BAAI/bge-large-zh-v1.5](https://huggingface.co/BAAI/bge-large-zh-v1.5) |
| `bge-reranker-base/` | BAAI 跨编码器重排模型（`RERANKER_ENABLED=true` 时启用） | 768 | 0.4 GB | [BAAI/bge-reranker-base](https://huggingface.co/BAAI/bge-reranker-base) |

> 本项目有**两个**本地模型依赖：嵌入模型（必选）+ 重排模型（`RERANKER_ENABLED=true` 时启用，
> `requirements.txt` 中 `FlagEmbedding==1.4.0` 即 bge-reranker-base 的封装）。LLM（DeepSeek / 通义千问）走 API，不在此处。
> 注意：`FlagEmbedding` 需在 **1.4.0+**（提供 `py3-none-any` 轮子）；1.2.x 仅有 sdist，Python 3.12 下无法二进制安装。

## 默认行为说明

- 后端 `EMBEDDING_MODEL_TYPE` 默认值为 `local`（sentence-transformers 本地嵌入模型）：
  使用 `EMBEDDING_MODEL_NAME` 指向的本地模型（默认 `BAAI/bge-large-zh-v1.5`，中文优化）。
  > 注：ChromaDB 已弃用，`chroma_default` 选项不再可用；原先的 ChromaDB 内置 ONNX 模型 `all-MiniLM-L6-v2` 已不再使用。
- 中文需求场景默认即使用本地 `bge-large-zh-v1.5`（中文优化、效果更好），无需额外切换；如需更换嵌入模型，仅修改 `EMBEDDING_MODEL_NAME` 即可。

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

## 通过宿主机 bind mount 挂载（当前部署方式，零运行时下载）

模型**不烘焙进镜像**，而是由 `docker-compose.yml` 以 `volume:ro` 从宿主机
`./models/...` 挂载进容器（后端 `EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5`、
`RERANKER_MODEL_NAME=/models/bge-reranker-base`）。
（背景：`./backend:/app` 的 volume 会覆盖镜像内 `/app/models`，故模型独立存放于宿主机
`/models` 并通过单独挂载。）

- 因此 **`docker compose` 部署需在构建前先把模型落到本目录**（见下方「重新下载」），
  容器启动后直接读取宿主机文件，运行时不再联网下载。
- **两个模型目录都必须存在且非空**：`bge-large-zh-v1.5`（嵌入，必选）与
  `bge-reranker-base`（重排，`RERANKER_ENABLED=true` 时启用）；任一缺失/为空都会导致
  对应模型加载失败、容器启动异常。

## 运行依赖

`local` 模式需要 `sentence-transformers` + `torch`（约数百 MB）。安装方式任选其一：

```bash
# 方式一：项目可选依赖
pip install -e ".[embedding]"

# 方式二：直接安装 requirements 中对应项
pip install sentence-transformers==3.1.1 torch==2.4.0
```

## 重新下载 / 切换源

### 统一入口（推荐）：`scripts/fetch-deps.py`

该脚本会下载**两个模型**到 `./models/`（Python 依赖由镜像构建时在线安装，无需预下载轮子）：

```bash
python scripts/fetch-deps.py --models-only     # 仅下载两个模型（推荐）
python scripts/fetch-deps.py                   # 模型 + 预热基础设施镜像
```

### 单独下载某个模型

底层仍调用 `scripts/download_model.py`（通过 `MODEL_REPO` / `LOCAL_DIR` 环境变量指定）：

```bash
# 嵌入模型（默认）
python scripts/download_model.py

# 重排模型
MODEL_REPO=BAAI/bge-reranker-base \
LOCAL_DIR=models/bge-reranker-base \
python scripts/download_model.py
```

> 默认走国内 HuggingFace 镜像（`hf-mirror.com`）；如需切换源可用 `HF_ENDPOINT` 覆盖。

也可用 ModelScope 源（国内更稳定）：

```bash
pip install modelscope
python -c "from modelscope import snapshot_download; \
snapshot_download('BAAI/bge-large-zh-v1.5', cache_dir='D:/git_down/ai-test-gen/models')"
python -c "from modelscope import snapshot_download; \
snapshot_download('BAAI/bge-reranker-base', cache_dir='D:/git_down/ai-test-gen/models')"
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
