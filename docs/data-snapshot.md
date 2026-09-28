# 知识库数据快照

源码仓库不保存知识库数据库和网页原文。使用
`scripts/package_handoff.py` 从受控知识库生成独立交接包，交接包包含：

- `data/knowledge.db`：SQLite 规范知识库
- `data/raw/`：知识库中引用的官方原始资料
- `HANDOFF.json`：交接包校验信息和文档统计

部署到目标主机时，将交接包中的 `data/` 目录复制为：

```text
/opt/zhixing-knowledge/data/
```

知识服务的环境变量应指向：

```text
KNOWLEDGE_DB=/opt/zhixing-knowledge/data/knowledge.db
KNOWLEDGE_RAW_DIR=/opt/zhixing-knowledge/data/raw
```

启动 Qdrant 和知识服务后，使用受控的 `KNOWLEDGE_API_KEY` 调用一次 `POST /reindex`，把 SQLite 中的资料重新建立为当前向量索引。模型权重、Qdrant 二进制和运行密钥不放在 Git 仓库中，需按根目录 `.env.example` 在目标主机单独安装和配置。
