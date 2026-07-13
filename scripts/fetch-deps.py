#!/usr/bin/env python3
# ============================================================================
# fetch-deps.py — 预下载 AI-Test-Gen 本地模型（Windows / Linux 通用）
#
# 背景：
#   - Python 依赖（torch / sentence-transformers / pymilvus 等）
#     在镜像构建时由 Dockerfile 直接 `pip install -r requirements.txt` 在线安装，
#     Docker 层缓存已能加速重复构建，无需（也不再支持）预先下载轮子缓存。
#   - 本脚本只需下载「无法用 pip 获取、且运行时以 bind mount 方式挂载进容器」
#     的本地模型权重：bge 中文嵌入模型 + bge-reranker 重排模型，
#     落盘到 ./models/，由 docker-compose.yml 以 volume:ro 挂载。
#   - （可选）预热基础设施镜像，使 docker pull 一次性拉取、后续离线。
#
# 用法（Windows 与 Linux 相同，只要 python 在 PATH）：
#   python scripts/fetch-deps.py                 # 两个模型 + 预热镜像
#   python scripts/fetch-deps.py --models-only    # 仅模型
#   python scripts/fetch-deps.py --no-pull        # 跳过 docker pull 预热
# ============================================================================

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_LOCAL = ROOT / "models" / "bge-large-zh-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-base"
RERANK_LOCAL = ROOT / "models" / "bge-reranker-base"

# 基础设施镜像（预热用）
INFRA_IMAGES = [
    "quay.io/coreos/etcd:v3.5.5",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z",
    "milvusdb/milvus:v2.4.0",
    "redis:7-alpine",
    "python:3.12-slim",
]


def _run(cmd, **kw):
    """打印并执行子命令；返回 CompletedProcess。"""
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw)


def ensure_hf_hub():
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("[info] 安装 huggingface_hub ...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"],
            check=True,
        )


def _model_complete(local: Path) -> bool:
    """模型是否已存在且完整（config.json + 至少一个非空权重文件）。"""
    cfg = local / "config.json"
    if not cfg.exists():
        return False
    weights = list(local.glob("*.safetensors")) + list(local.glob("pytorch_model.bin"))
    return any(w.stat().st_size > 0 for w in weights)


def download_model(repo: str, local: Path) -> bool:
    print(f"  - {repo} -> {local}")
    if _model_complete(local):
        print("    [skip] 模型已存在且完整，跳过下载（删除该目录可强制重新下载）")
        return True
    local.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["MODEL_REPO"] = repo
    env["LOCAL_DIR"] = str(local)
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "download_model.py")],
            env=env,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        print(f"    [warn] 模型 {repo} 下载失败：所有 HF_ENDPOINT 均不可达。")
        print(f"          排查：检查网络/代理（如设置 HTTPS_PROXY / HF_ENDPOINT），")
        print(f"          或手动将模型放到 {local} 后重跑本脚本（会幂等跳过）。")
        return False


def download_models() -> bool:
    print("==> 下载本地模型到 models/（运行时以 bind mount 挂载进容器，必须存在）")
    ensure_hf_hub()
    r1 = download_model(EMBED_MODEL, EMBED_LOCAL)
    r2 = download_model(RERANK_MODEL, RERANK_LOCAL)
    if not (r1 and r2):
        print("    [warn] 模型未全部就绪：docker-compose.yml 会以 volume:ro 挂载")
        print("           ./models/bge-large-zh-v1.5 与 ./models/bge-reranker-base，")
        print("           目录缺失或为空将导致容器启动后加载模型失败。")
    return r1 and r2


def pull_images() -> bool:
    docker = shutil.which("docker")
    if not docker:
        print("[warn] 未检测到 docker，跳过镜像预热。")
        return True  # 非必需步骤，不计为失败
    print("==> 预热基础设施镜像")
    all_ok = True
    for img in INFRA_IMAGES:
        print(f"  - {img}")
        r = subprocess.run(
            [docker, "pull", img],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            print("    [ok]")
        else:
            print("    [skip] 拉取失败（可稍后重试，非必需）")
            all_ok = False
    return all_ok


def main():
    p = argparse.ArgumentParser(
        description="预下载 AI-Test-Gen 本地模型（Windows/Linux 通用）"
    )
    p.add_argument("--models-only", action="store_true", help="仅下载本地模型")
    p.add_argument("--no-pull", action="store_true", help="跳过 docker pull 预热")
    args = p.parse_args()

    do_models = True
    do_pull = not (args.models_only or args.no_pull)

    failures = []
    if do_models and not download_models():
        failures.append("models")
    if do_pull:
        pull_images()  # 预热为可选项，不影响退出码

    print("\n==> 完成。")
    if failures:
        print(f"[warn] 以下组件未成功：{', '.join(failures)}。可单独重跑对应步骤。")
        print("    例：python scripts/fetch-deps.py --models-only")
    else:
        print("[ok] 所有请求的组件已就绪。")
    print("    下一步：")
    print("    docker compose -f docker-compose.base.yml up -d etcd minio milvus")
    print("    docker compose -f docker-compose.base.yml build base")
    print("    docker compose up -d --build")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
