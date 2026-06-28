# Memory Recall Evaluation Dataset

运行：

```powershell
python evals/generate_recall_dataset.py
```

生成内容：

- `generated/memory_snapshot.jsonl`：canonical memory 快照。
- `generated/recall_cases.jsonl`：召回黄金用例。
- `generated/*.csv`：供飞书表格同步与人工检查。
- `.sandbox/evals/memory_eval.db`：隔离的本地 Memory Runtime。

评测库与日常运行使用的 `.sandbox/memory.db` 分离，避免模拟数据污染真实 PoC
记忆。数据生成器使用稳定 ID，可重复运行。

## 运行 SQLite FTS 基线

```powershell
python evals/run_recall_eval.py
```

输出位于：

- `reports/sqlite_fts/baseline.json`：完整机器可读报告。
- `reports/sqlite_fts/baseline_cases.csv`：逐用例结果，适合筛选失败样本。
- `reports/sqlite_fts/baseline.md`：便于人工阅读的指标与失败摘要。

可按类别或数量局部运行：

```powershell
python evals/run_recall_eval.py --category governance --limit 10
```

## 比较自研召回方案

评测框架不依赖第三方记忆项目。后续增加自研向量召回、混合召回或
LLM reranker 时，为每种配置输出独立报告，再使用通用比较工具：

```powershell
python evals/compare_recall_reports.py \
  evals/reports/sqlite_fts/baseline.json \
  evals/reports/vector_local/baseline.json \
  evals/reports/hybrid_rrf/baseline.json
```

所有方案必须读取同一份 canonical memory 快照，并输出统一的召回协议。
