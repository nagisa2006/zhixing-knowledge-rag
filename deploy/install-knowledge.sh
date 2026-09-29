#!/usr/bin/env bash
# 小电知识服务：直接部署到脚本所在的代码目录
#
#   sudo bash /path/to/knowledge/deploy/install-knowledge.sh <wheel目录> <requirements文件>
#
# 代码目录由脚本位置决定；两个参数仅用于安装 Python 依赖。
#
# 每一步都带状态判断，可以重复执行：已经做完的跳过，中断的补上。
# 已有的 data/ 和 knowledge.env 不会被覆盖 —— 前者可能已被摄取修改过，
# 后者的 API key 可能已经抄进后端配置。
#
# 不做的事：不建立向量索引（耗时长，单独跑）、不改后端 backend.env、
# 不启用官方刷新与论坛同步 timer。这三件由人决定。
set -euo pipefail

KS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$#" -eq 2 ] || {
  echo "用法：sudo bash $0 <wheel目录> <requirements文件>" >&2
  exit 1
}
WHEEL_DIR="$1"
REQUIREMENTS_FILE="$(realpath "$2")"

[ "$(id -u)" -eq 0 ] || { echo "!! 需要 root：sudo bash $0" >&2; exit 1; }

echo "=== 0. 前置检查 ==="
echo "代码目录：$KS"
echo "wheel 目录：$WHEEL_DIR"
echo "requirements：$REQUIREMENTS_FILE"

missing=0
for path in "$WHEEL_DIR" "$REQUIREMENTS_FILE" \
            "$KS/data/knowledge.db" "$KS/knowledge_service/server.py"; do
  [ -e "$path" ] || { echo "!! 缺少：$path" >&2; missing=1; }
done
models_missing=0
for model in bge-m3 bge-reranker-base; do
  [ -d "$KS/models/$model" ] || {
    echo "!! 缺少模型目录：$KS/models/$model" >&2
    missing=1
    models_missing=1
  }
done
[ "$models_missing" -eq 0 ] || {
  echo "   自动下载：sudo bash $KS/deploy/download-models.sh" >&2
  echo "   详情见：$KS/README.md 的“下载并放置模型”章节" >&2
}
if [ ! -x "$KS/qdrant/qdrant" ] && ! command -v qdrant >/dev/null 2>&1; then
  echo "!! 缺少 Qdrant：请将可执行文件放到 $KS/qdrant/qdrant，或安装到系统 PATH" >&2
  missing=1
fi
[ "$missing" -eq 0 ] || exit 1

python3 -c "import sys; assert sys.version_info[:2]==(3,10), sys.version" \
  || { echo "!! 需要 Python 3.10（离线 wheel 是按 3.10 解析的）" >&2; exit 1; }

echo "前置检查通过"

echo
echo "=== 1. 服务用户 ==="
if id zhixing >/dev/null 2>&1; then
  echo "zhixing 已存在，跳过"
else
  # systemd 单元里写的是 User=zhixing；这台机器上原本没有这个用户。
  useradd --system --home-dir "$KS" --no-create-home --shell /usr/sbin/nologin zhixing
  echo "已建立系统用户 zhixing（nologin）"
fi

echo
echo "=== 2. 代码与数据 ==="
echo "代码已在目标目录，跳过复制（不覆盖现有 data/）"
if command -v sqlite3 >/dev/null 2>&1; then
  echo "  已发布文档：$(sqlite3 "$KS/data/knowledge.db" "SELECT count(*) FROM documents WHERE status='published';")"
fi
echo "  原始资料：$(find "$KS/data/raw" -type f | wc -l) 份"

echo
echo "=== 3. 模型 ==="
echo "模型已就位"
for m in bge-m3 bge-reranker-base; do
  echo "  $m: $(du -sh "$KS/models/$m" | cut -f1)"
done

echo
echo "=== 4. Qdrant ==="
mkdir -p "$KS/qdrant/storage" "$KS/qdrant/snapshots"
if [ -x "$KS/qdrant/qdrant" ]; then
  echo "二进制已就位，跳过"
elif command -v qdrant >/dev/null 2>&1; then
  install -m 0755 "$(command -v qdrant)" "$KS/qdrant/qdrant"
  echo "已从系统 PATH 复制 qdrant"
else
  echo "!! 缺少 Qdrant：请将可执行文件放到 $KS/qdrant/qdrant，或安装到系统 PATH" >&2
  exit 1
fi
echo "Qdrant 已就位"

echo
echo "=== 5. Python 环境 ==="
# 依赖直接安装到系统 Python，不建立或使用虚拟环境。
# 依赖校验只用 importlib.metadata。ks-offline/install.sh 那份用的是
# qdrant_client.__version__，而 qdrant-client 1.15.1 没有这个属性，
# 会在依赖其实装好的情况下以 AttributeError 退出。
verify_python() {
  python3 - <<'PY'
import importlib.util, sys
from importlib.metadata import version
mods = ["jieba","numpy","torch","onnxruntime","qdrant_client","sentence_transformers",
        "transformers","trafilatura","pypdf","docx","openpyxl","cv2"]
missing = [m for m in mods if not importlib.util.find_spec(m)]
if missing:
    print("缺少模块:", ", ".join(missing)); sys.exit(1)
import jieba
print("  jieba 分词:", jieba.lcut("国家励志奖学金"))
# 版本号只是给人看的。取不到不代表装得不对，不能让它决定安装成败。
for dist in ["numpy","torch","onnxruntime","qdrant-client","sentence-transformers",
             "transformers","trafilatura","pypdf","python-docx","openpyxl","opencv-python"]:
    try:
        print(f"  {dist} {version(dist)}")
    except Exception as error:
        print(f"  {dist} 版本未知（{type(error).__name__}）")
print("ALL_IMPORTS_OK")
PY
}

if verify_python; then
  echo "系统 Python 依赖已可用，跳过安装"
else
  echo "向系统 Python 安装依赖（不建立 venv）"
  # requirements 中包含 jieba；wheel 目录应包含它及其他依赖的 wheel 或源码包。
  python3 -m pip install --quiet --no-index --no-deps \
      --find-links="$WHEEL_DIR" -r "$REQUIREMENTS_FILE"
  verify_python
fi

# 旧版本曾在此处创建 venv；服务已切换到系统 Python，删除这个已废弃的副本。
rm -rf "$KS/venv"

echo
echo "=== 6. 配置 ==="
if [ -f "$KS/knowledge.env" ]; then
  echo "knowledge.env 已存在，保留不动（其中的 API key 可能已抄进后端配置）"
  key="$(sed -n 's/^KNOWLEDGE_API_KEY=//p' "$KS/knowledge.env" | head -1)"
else
  sed "s#/opt/zhixing-knowledge#$KS#g" "$KS/.env.example" > "$KS/knowledge.env"
  key="$(openssl rand -hex 32)"
  sed -i "s|^KNOWLEDGE_API_KEY=.*|KNOWLEDGE_API_KEY=$key|" "$KS/knowledge.env"
  echo "已生成 knowledge.env，KNOWLEDGE_API_KEY 已填随机值"
  echo "请配置社区来源 URL 和独立同步密钥后再启用社区同步"
fi
# 仅迁移模板中的旧默认根目录；其他用户自定义路径保持不变。
sed -i "s#/opt/zhixing-knowledge#$KS#g" "$KS/knowledge.env"
chmod 600 "$KS/knowledge.env"

echo
echo "=== 7. 归属与 systemd ==="
chown -R zhixing:zhixing "$KS"
# 服务模板中的路径按实际代码目录展开，不再写死 /opt/zhixing-knowledge。
sed "s#/opt/zhixing-knowledge#$KS#g" \
    "$KS/deploy/qdrant.service" > /etc/systemd/system/qdrant.service
sed "s#/opt/zhixing-knowledge#$KS#g" \
    "$KS/deploy/zhixing-knowledge.service" > /etc/systemd/system/zhixing-knowledge.service
sed "s#/opt/zhixing-knowledge#$KS#g" \
    "$KS/deploy/qdrant.yaml" > "$KS/qdrant/qdrant.yaml"
systemctl daemon-reload
echo "已安装指向 $KS 的 qdrant.service 与 zhixing-knowledge.service"
echo "自动刷新和论坛同步未进入正式部署包"

echo
echo "=== 8. 启动 ==="
# 起不来时要看到日志再退出，否则 set -e 会在最需要信息的时候静默结束。
show_state() {
  systemctl status qdrant zhixing-knowledge --no-pager -l 2>&1 | sed -n '1,40p' || true
  echo "--- zhixing-knowledge 最近日志 ---"
  journalctl -u zhixing-knowledge -n 40 --no-pager 2>&1 || true
}
if ! systemctl enable --now qdrant; then
  echo "!! qdrant 启动失败" >&2; show_state; exit 1
fi
sleep 3
if ! systemctl enable zhixing-knowledge || ! systemctl restart zhixing-knowledge; then
  echo "!! zhixing-knowledge 启动失败" >&2; show_state; exit 1
fi
sleep 5
if ! systemctl is-active --quiet zhixing-knowledge; then
  echo "!! zhixing-knowledge 启动后又退出了" >&2; show_state; exit 1
fi
show_state

echo
echo "=== 安装完成。还没做的三件事 ==="
cat <<NEXT

1) 建立向量索引（SQLite 里有资料，Qdrant 还是空的）。567 篇在 CPU 上较慢，放 tmux 里跑：

   source $KS/knowledge.env
   curl -fsS --max-time 7200 -X POST http://127.0.0.1:8091/reindex \\
        -H "Authorization: Bearer \$KNOWLEDGE_API_KEY"

2) 后端对接。编辑 /home/ubuntu/treehole/shared/config/backend.env：

   BACKEND_RAG_URL=http://127.0.0.1:8091
   BACKEND_RAG_API_KEY=$key

   然后：sudo systemctl restart treehole

3) 验收（第三条失败时不报错，必须单独验）：

   curl -fsS http://127.0.0.1:8091/health
   curl -fsS http://127.0.0.1:8091/status -H "Authorization: Bearer \$KNOWLEDGE_API_KEY"
   curl -fsS -X POST http://127.0.0.1:8091/search \\
        -H "Authorization: Bearer \$KNOWLEDGE_API_KEY" \\
        -H 'Content-Type: application/json' \\
        -d '{"query":"国家励志奖学金申请条件","limit":5}'

   # 取上面结果里的 documentId，确认原文回读真的读到了原始文件：
   curl -fsS "http://127.0.0.1:8091/documents/<documentId>/content" \\
        -H "Authorization: Bearer \$KNOWLEDGE_API_KEY" | head -c 400

   返回的 content 应当明显长于检索片段。两者长度接近说明原文没读到。
NEXT
