#!/usr/bin/env bash
# 下载知识服务固定版本的 embedding 和 reranker 模型。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="${1:-$ROOT/models}"
PYTHON="${PYTHON:-python3}"

command -v "$PYTHON" >/dev/null 2>&1 || {
  echo "!! 找不到 Python：$PYTHON" >&2
  exit 1
}

mkdir -p "$MODEL_DIR"
MODEL_DIR="$(cd "$MODEL_DIR" && pwd)"

tmp=""
cleanup() {
  [ -z "$tmp" ] || rm -rf "$tmp"
}
trap cleanup EXIT

if ! "$PYTHON" -c 'import huggingface_hub' >/dev/null 2>&1; then
  echo "未安装 huggingface-hub，临时下载工具依赖"
  tmp="$(mktemp -d)"
  "$PYTHON" -m pip install --quiet --target "$tmp" huggingface-hub
  export PYTHONPATH="$tmp${PYTHONPATH:+:$PYTHONPATH}"
fi

echo "模型目录：$MODEL_DIR"
MODEL_DIR="$MODEL_DIR" "$PYTHON" - <<'PY'
import os
from pathlib import Path

from huggingface_hub import snapshot_download

root = Path(os.environ["MODEL_DIR"])
models = [
    {
        "repo_id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "directory": "bge-m3",
        "ignore_patterns": [
            "onnx/*",
            "imgs/*",
            "*.jpg",
            ".gitattributes",
            "README.md",
            "colbert_linear.pt",
            "sparse_linear.pt",
        ],
    },
    {
        "repo_id": "BAAI/bge-reranker-base",
        "revision": "2cfc18c9415c912f9d8155881c133215df768a70",
        "directory": "bge-reranker-base",
        "ignore_patterns": [
            "onnx/*",
            ".gitattributes",
            "README.md",
            "pytorch_model.bin",
        ],
    },
]

for model in models:
    destination = root / model["directory"]
    print(f"下载 {model['repo_id']}@{model['revision']} -> {destination}", flush=True)
    snapshot_download(
        repo_id=model["repo_id"],
        revision=model["revision"],
        local_dir=destination,
        ignore_patterns=model["ignore_patterns"],
    )

required = [
    root / "bge-m3/config.json",
    root / "bge-m3/pytorch_model.bin",
    root / "bge-reranker-base/config.json",
    root / "bge-reranker-base/model.safetensors",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit("下载不完整，缺少：\n" + "\n".join(missing))

print("模型下载完成")
PY
