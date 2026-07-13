"""
下载 AI-Test-Gen 所需的本地模型到项目 models/ 目录，便于离线 / 本地部署。

用法（在隔离 venv 中）：
    python scripts/download_model.py

环境变量：
    HF_ENDPOINT   可选。HuggingFace 镜像源；未设置时依次尝试官方源与国内镜像。
    MODEL_REPO    HuggingFace 仓库名（默认 BAAI/bge-large-zh-v1.5）
    LOCAL_DIR     本地落盘目录（默认 ./models/bge-large-zh-v1.5）

行为：
    - 若设置了 HF_ENDPOINT，优先使用该源；
    - 否则依次尝试官方源 https://huggingface.co 与国内镜像 https://hf-mirror.com，
      任一可达即下载成功（避免单一镜像不可达时直接失败）。
"""
import os
import sys

MODEL_REPO = os.environ.get("MODEL_REPO", "BAAI/bge-large-zh-v1.5")

# 项目根目录：scripts/ 的上一级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.environ.get(
    "LOCAL_DIR",
    os.path.join(PROJECT_ROOT, "models", "bge-large-zh-v1.5"),
)

# 镜像源候选顺序：用户指定优先 -> 官方源 -> 国内镜像（任一可用即可）
_user_ep = os.environ.get("HF_ENDPOINT")
CANDIDATES = []
if _user_ep:
    CANDIDATES.append(_user_ep)
if "https://huggingface.co" not in CANDIDATES:
    CANDIDATES.append("https://huggingface.co")
if "https://hf-mirror.com" not in CANDIDATES:
    CANDIDATES.append("https://hf-mirror.com")

print(f"[info] MODEL_REPO  = {MODEL_REPO}")
print(f"[info] LOCAL_DIR   = {LOCAL_DIR}")
print(f"[info] 将依次尝试 HF_ENDPOINT: {CANDIDATES}")


def do_download(endpoint: str):
    """尝试从指定 endpoint 下载；成功返回路径，失败返回 None。"""
    os.environ["HF_ENDPOINT"] = endpoint
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(repo_id=MODEL_REPO, local_dir=LOCAL_DIR)
    except Exception as e:  # noqa: BLE001 — 多源回退，需捕获所有异常
        msg = str(e).replace("\n", " ")[:160]
        print(f"    [fail] {endpoint} -> {type(e).__name__}: {msg}")
        return None


for ep in CANDIDATES:
    print(f"[info] 尝试 HF_ENDPOINT = {ep}")
    path = do_download(ep)
    if path:
        print("[done] 模型已下载到：", path)
        sys.exit(0)

print("[error] 所有 HF_ENDPOINT 均不可达。请检查网络 / 代理，或手动将模型放到", LOCAL_DIR)
sys.exit(1)
