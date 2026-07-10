"""
下载 AI-Test-Gen 所需的本地嵌入模型 BAAI/bge-large-zh-v1.5
到项目 models/ 目录，便于离线 / 本地部署。

用法（在隔离 venv 中）：
    python scripts/download_model.py

环境变量：
    HF_ENDPOINT   HuggingFace 镜像源（默认 https://hf-mirror.com，国内加速）
    MODEL_REPO    HuggingFace 仓库名（默认 BAAI/bge-large-zh-v1.5）
    LOCAL_DIR     本地落盘目录（默认 ./models/bge-large-zh-v1.5）
"""
import os
import sys

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
MODEL_REPO = os.environ.get("MODEL_REPO", "BAAI/bge-large-zh-v1.5")

# 项目根目录：scripts/ 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.environ.get(
    "LOCAL_DIR",
    os.path.join(PROJECT_ROOT, "models", "bge-large-zh-v1.5"),
)

# 如需更高下载性能，可安装 xet 后启用 HF_XET_HIGH_PERFORMANCE=1
os.environ["HF_ENDPOINT"] = HF_ENDPOINT

print(f"[info] HF_ENDPOINT = {HF_ENDPOINT}")
print(f"[info] MODEL_REPO  = {MODEL_REPO}")
print(f"[info] LOCAL_DIR   = {LOCAL_DIR}")

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[error] 未安装 huggingface_hub，请先执行：pip install huggingface_hub")
    sys.exit(1)

path = snapshot_download(
    repo_id=MODEL_REPO,
    local_dir=LOCAL_DIR,
)

print("[done] 模型已下载到：", path)
