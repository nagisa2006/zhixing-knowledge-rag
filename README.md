# 小电知识服务：首次安装

目标主机 `ubuntu-KVM`（`zhixing.xidian.edu.cn`）。这台机器**没有外网**，堡垒机也**禁用了
SSH exec 通道**，所以不要在服务器上执行 `git pull` —— `/home/ubuntu/treehole` 不是 Git
工作区，取不到代码。本包已经把代码和数据都带齐。

模型**不在本包内**，因为主机上已经有了，不需要再传 3.4 GB。

## 本包内容

```text
zhixing-knowledge/
  current/knowledge_service/   服务代码
  current/deploy/              Qdrant 与 systemd 配置
  current/.env.example         配置模板
  data/knowledge.db            规范知识库
  data/raw/                    官方原始资料快照
  README.md                    部署说明
  HANDOFF.json                 交接包校验信息
```

在知识仓库根目录生成交接包：

```bash
python scripts/package_handoff.py \
  --db /path/to/knowledge.db \
  --output dist \
  --version <release-version>
```

输出 `zhixing-knowledge-<release-version>.tar.gz` 及 SHA-256 sidecar；社区帖子、
检索 trace、任务记录和运行密钥不会进入交接包。

## 0. 一键安装（推荐）

`install-knowledge.sh` 安装代码、数据、模型、Qdrant、Python 环境和 systemd 服务；
它不会建立向量索引，也不会修改业务后端配置。

```bash
sudo bash /home/ubuntu/treehole/incoming/install-knowledge.sh
```

先读一遍再跑。想手动逐步来就跳过本节，按第 1 步往下。

## 1. 服务用户与解包

systemd 单元里写的是 `User=zhixing`，**这台机器上原本没有这个用户**，要先建：

```bash
sudo useradd --system --home-dir /opt/zhixing-knowledge --no-create-home \
             --shell /usr/sbin/nologin zhixing

sudo tar -C /opt --no-same-owner -xzf /home/ubuntu/treehole/incoming/zhixing-knowledge-<版本>.tar.gz
```

`--no-same-owner` 不能省：包是在 macOS 上打的，里面带着那边的 uid/gid（501/staff），以 root
解包会把这些数字原样搬过来，而主机上没有对应用户。第 7 步的 `chown` 会纠正，但中途失败就会
留下一地属主错误的文件。

## 2. 放置模型（用主机上已有的那份）

模型已在 `/home/ubuntu/treehole/incoming/ks-offline/models/`，9 月 10 日就位，固定修订与
`.env.example` 一致（`bge-m3` 为 `5617a9f6…`，`bge-reranker-base` 为
`2cfc18c9…`），且已按运行所需裁剪。

```bash
sudo cp -a /home/ubuntu/treehole/incoming/ks-offline/models /opt/zhixing-knowledge/models
sudo chown -R zhixing:zhixing /opt/zhixing-knowledge/models
```

## 3. Python 环境

安装脚本直接把依赖安装到系统 Python，不建立虚拟环境；systemd 也使用 `/usr/bin/python3`。
默认从 `ks-offline/wheels/` 读取 wheel，也可以把自定义 wheel 目录作为第二个参数传入：

```bash
sudo bash /home/ubuntu/treehole/incoming/install-knowledge.sh \
     /home/ubuntu/treehole/incoming/zhixing-knowledge-<hash>.tar.gz \
     /path/to/wheels
```

也可以通过 `WHEEL_DIR` 环境变量指定目录。安装过程仍然全程离线：wheel 用指定目录，
`jieba` 使用 `ks-offline/sdist/jieba-0.42.1.tar.gz`。

依赖校验只使用 `importlib.metadata`。不要用 `ks-offline/install.sh`，它结尾打印
`qdrant_client.__version__`，而 qdrant-client 1.15.1 没有这个属性，会在依赖已经装好的
情况下以 `AttributeError` 退出。

已核对 `~/ks-env` 里的依赖与 `knowledge_service/requirements.txt` 逐项一致
（torch 2.6.0+cpu、sentence-transformers 4.1.0、transformers 4.51.3、qdrant-client 1.15.1、
jieba 0.42.1、lxml 6.1.3、numpy 1.26.4 等），所以离线包是完整的。

注意部署文档写的是 Python 3.11，本机只有 3.10，全部依赖在 3.10 可得，离线包也是按 3.10
准备的。

## 4. Qdrant

主机上的 Qdrant 是压缩包（`qdrant-x86_64-unknown-linux-gnu.tar.gz`），不是解好的二进制，
要先解出来：

```bash
sudo mkdir -p /opt/zhixing-knowledge/qdrant/{storage,snapshots}
tmp=$(mktemp -d)
tar -C "$tmp" -xzf /home/ubuntu/treehole/incoming/ks-offline/qdrant/qdrant-x86_64-unknown-linux-gnu.tar.gz
sudo install -m 0755 "$(find "$tmp" -type f -name qdrant | head -1)" /opt/zhixing-knowledge/qdrant/qdrant
rm -rf "$tmp"
sudo cp /opt/zhixing-knowledge/current/deploy/qdrant.yaml /opt/zhixing-knowledge/qdrant/
sudo chown -R zhixing:zhixing /opt/zhixing-knowledge/qdrant
```

`qdrant.yaml` 里 `http_port` 是 6333，与 `KNOWLEDGE_QDRANT_URL` 一致；storage/snapshots 路径
也已指向上面建的目录。

## 5. 配置

```bash
sudo cp /opt/zhixing-knowledge/current/.env.example /opt/zhixing-knowledge/knowledge.env
sudo chmod 600 /opt/zhixing-knowledge/knowledge.env
sudo chown zhixing:zhixing /opt/zhixing-knowledge/knowledge.env
sudo nano /opt/zhixing-knowledge/knowledge.env
```

必须确认或填写：

```text
KNOWLEDGE_DB=/opt/zhixing-knowledge/data/knowledge.db
KNOWLEDGE_RAW_DIR=/opt/zhixing-knowledge/data/raw
KNOWLEDGE_EMBEDDING_MODEL=/opt/zhixing-knowledge/models/bge-m3
KNOWLEDGE_RERANK_MODEL=/opt/zhixing-knowledge/models/bge-reranker-base
KNOWLEDGE_RERANK_THRESHOLD=0.45
KNOWLEDGE_API_KEY=<自行生成，必须与后端 BACKEND_RAG_API_KEY 一致>
KNOWLEDGE_COMMUNITY_SOURCE_URL=http://127.0.0.1:8080/api/internal/knowledge/community-posts
KNOWLEDGE_COMMUNITY_SOURCE_API_KEY=<必须与后端 BACKEND_KNOWLEDGE_SYNC_API_KEY 一致>
```

`KNOWLEDGE_RERANK_THRESHOLD` 的 0.45 是针对上面这个 `bge-reranker-base` 修订、在固定本地
检索用例上标定的。**不要沿用任何来自 `bge-reranker-v2-m3` 的旧值（如 0.05）** —— 两个模型
分数尺度不同，旧值会让几乎所有候选通过，等于重排失效。

`KNOWLEDGE_RAW_DIR` 必须指向真实的 `data/raw`。服务按这个变量定位原始资料，配错会导致原文
回读静默失败：接口仍返回 200，但回答只用到规范正文。

## 6. systemd

```bash
sudo cp /opt/zhixing-knowledge/current/deploy/qdrant.service \
        /opt/zhixing-knowledge/current/deploy/zhixing-knowledge.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qdrant
sudo systemctl enable --now zhixing-knowledge
sudo systemctl status qdrant zhixing-knowledge --no-pager
```


## 7. 建立向量索引

SQLite 里有资料，但 Qdrant 是空的，必须建一次索引：

```bash
source /opt/zhixing-knowledge/knowledge.env
curl -fsS -X POST http://127.0.0.1:8091/reindex \
     -H "Authorization: Bearer $KNOWLEDGE_API_KEY"
```

567 篇在 CPU 上建索引耗时较长，建议放 `tmux` 里跑。

## 8. 后端对接

后端配置在 `/home/ubuntu/treehole/shared/config/backend.env`：

```text
BACKEND_RAG_URL=http://127.0.0.1:8091
BACKEND_RAG_API_KEY=<与 KNOWLEDGE_API_KEY 完全一致>
BACKEND_KNOWLEDGE_SYNC_API_KEY=<与 KNOWLEDGE_COMMUNITY_SOURCE_API_KEY 完全一致>
```

改完重启后端：`sudo systemctl restart treehole`。

后端重启并确认内部接口可达后，首次同步公开社区帖子：

```bash
sudo -u zhixing bash -c '
  set -a
  source /opt/zhixing-knowledge/knowledge.env
  set +a
  cd /opt/zhixing-knowledge/current
  /usr/bin/python3 -m knowledge_service.community
'
```

同步只接收公开且审核通过的正文；其他帖子只以不含正文的 tombstone 触发索引禁用。

## 9. 验收

```bash
# 进程是否起来（/health 不需要鉴权）
curl -fsS http://127.0.0.1:8091/health

# 索引状态（需要鉴权）
curl -fsS http://127.0.0.1:8091/status -H "Authorization: Bearer $KNOWLEDGE_API_KEY"

# 检索是否有结果
curl -fsS -X POST http://127.0.0.1:8091/search \
     -H "Authorization: Bearer $KNOWLEDGE_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"query":"国家励志奖学金申请条件","limit":5}'
```

**原文回读必须单独验一次**，它失败时不报错：

```bash
# 从上面 search 结果里取一个 documentId
curl -fsS "http://127.0.0.1:8091/documents/<documentId>/content" \
     -H "Authorization: Bearer $KNOWLEDGE_API_KEY" | head -c 400
```

返回的 `content` 应该明显长于该文档在检索结果里的片段。如果两者长度接近，说明
`KNOWLEDGE_RAW_DIR` 配错或 `data/raw/` 没拷全，原文没有真正被读到。
