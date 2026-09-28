#!/usr/bin/env bash
# 小电知识服务：安装到 /opt/zhixing-knowledge
#
 #   sudo bash /home/ubuntu/treehole/incoming/install-knowledge.sh [包路径] [wheel目录]
#
# 每一步都带状态判断，可以重复执行：已经做完的跳过，中断的补上。
# 已有的 data/ 和 knowledge.env 不会被覆盖 —— 前者可能已被摄取修改过，
# 后者的 API key 可能已经抄进后端配置。
#
# 不做的事：不建立向量索引（耗时长，单独跑）、不改后端 backend.env、
# 不启用官方刷新与论坛同步 timer。这三件由人决定。
set -euo pipefail

KS=/opt/zhixing-knowledge
INCOMING=/home/ubuntu/treehole/incoming
OFFLINE="$INCOMING/ks-offline"
# 包名带构建哈希，而这个脚本自己也在包里，写死名字必然会过期。
# 默认取 incoming/ 里最新的那个，也可以显式指定。
PKG="${1:-$(ls -t "$INCOMING"/zhixing-knowledge-*.tar.gz 2>/dev/null | head -1)}"
# 默认使用离线包自带的 wheels，也可以通过第二个参数或 WHEEL_DIR 指定目录。
WHEEL_DIR="${2:-${WHEEL_DIR:-$OFFLINE/wheels}}"

[ "$(id -u)" -eq 0 ] || { echo "!! 需要 root：sudo bash $0" >&2; exit 1; }
[ -n "$PKG" ] || { echo "!! $INCOMING 里找不到 zhixing-knowledge-*.tar.gz" >&2; exit 1; }

echo "=== 0. 前置检查 ==="
echo "安装包：$PKG"

for path in "$PKG" "$PKG.sha256" \
            "$WHEEL_DIR" "$OFFLINE/pinned.txt" \
            "$OFFLINE/sdist/jieba-0.42.1.tar.gz" \
            "$OFFLINE/models/bge-m3" \
            "$OFFLINE/models/bge-reranker-base" \
            "$OFFLINE/qdrant/qdrant-x86_64-unknown-linux-gnu.tar.gz"; do
  [ -e "$path" ] || { echo "!! 缺少：$path" >&2; exit 1; }
done

# sha256 旁文件里的名字是相对的，必须在同目录校验
(cd "$(dirname "$PKG")" && sha256sum -c "$(basename "$PKG").sha256")

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
if [ -f "$KS/data/knowledge.db" ] && [ -f "$KS/current/knowledge_service/server.py" ]; then
  echo "已解包，跳过（不覆盖现有 data/）"
else
  mkdir -p "$KS"
  # --no-same-owner：包是在 macOS 上打的，带着那边的 uid/gid（501/staff）。
  # 以 root 解包会把这些数字原样搬过来，而主机上没有这个用户。
  tar -C /opt --no-same-owner -xzf "$PKG"
  echo "已解包到 $KS"
fi
if command -v sqlite3 >/dev/null 2>&1; then
  echo "  已发布文档：$(sqlite3 "$KS/data/knowledge.db" "SELECT count(*) FROM documents WHERE status='published';")"
fi
echo "  原始资料：$(find "$KS/data/raw" -type f | wc -l) 份"

echo
echo "=== 3. 模型（用主机上已有的，不重新传输）==="
if [ -d "$KS/models/bge-m3" ] && [ -d "$KS/models/bge-reranker-base" ]; then
  echo "已就位，跳过"
else
  cp -a "$OFFLINE/models" "$KS/models"
fi
for m in bge-m3 bge-reranker-base; do
  echo "  $m: $(du -sh "$KS/models/$m" | cut -f1)"
done

echo
echo "=== 4. Qdrant ==="
mkdir -p "$KS/qdrant/storage" "$KS/qdrant/snapshots"
if [ -x "$KS/qdrant/qdrant" ]; then
  echo "二进制已就位，跳过"
else
  tmp="$(mktemp -d)"
  tar -C "$tmp" -xzf "$OFFLINE/qdrant/qdrant-x86_64-unknown-linux-gnu.tar.gz"
  bin="$(find "$tmp" -maxdepth 2 -type f -name qdrant | head -1)"
  [ -n "$bin" ] || { echo "!! 压缩包里找不到 qdrant 可执行文件" >&2; rm -rf "$tmp"; exit 1; }
  install -m 0755 "$bin" "$KS/qdrant/qdrant"
  rm -rf "$tmp"
fi
cp "$KS/current/deploy/qdrant.yaml" "$KS/qdrant/qdrant.yaml"
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
  python3 -m pip install --quiet --no-index --no-deps \
      --find-links="$WHEEL_DIR" -r "$OFFLINE/pinned.txt"
  # jieba 只有源码包；--no-build-isolation 用已装的 setuptools，避免联网取构建依赖
  python3 -m pip install --quiet --no-index --no-build-isolation \
      "$OFFLINE/sdist/jieba-0.42.1.tar.gz"
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
  cp "$KS/current/.env.example" "$KS/knowledge.env"
  key="$(openssl rand -hex 32)"
  sed -i "s|^KNOWLEDGE_API_KEY=.*|KNOWLEDGE_API_KEY=$key|" "$KS/knowledge.env"
  echo "已生成 knowledge.env，KNOWLEDGE_API_KEY 已填随机值"
  echo "请配置社区来源 URL 和独立同步密钥后再启用社区同步"
fi
chmod 600 "$KS/knowledge.env"

echo
echo "=== 7. 归属与 systemd ==="
# 包是 macOS 打的，解包可能留下不存在的 uid；这里统一纠正。
chown -R zhixing:zhixing "$KS"
cp "$KS/current/deploy/qdrant.service" "$KS/current/deploy/zhixing-knowledge.service" /etc/systemd/system/
systemctl daemon-reload
echo "已安装 qdrant.service 与 zhixing-knowledge.service"
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
