# Project Sora Context Catalog

本目录是 `PROJECT_LEDGER.md` 的机器可读索引。它解决三个问题：

1. 新任务快速定位一个系统的设计稿、代码、测试、审计和当前状态。
2. 自动化检查规划、实现、验收和发布证据是否断链。
3. 避免把所有信息塞进一个不断追加的 Markdown 文件。

## Catalogs

- `systems.json`: 系统边界、成熟度、代码与测试入口、设计和审计文档。
- `artifacts.json`: 飞书文档、仓库文档及其权威性和确认状态。
- `milestones.json`: 以 Git 提交为证据的已交付工程里程碑。
- `open-items.json`: 尚未完成、需要验证或刻意延期的工作。

## Status Vocabulary

- `implemented_baseline`: 主要领域能力、存储、契约和测试已经实现，但不等于产品发布完成。
- `integration_in_progress`: 后端或规格已存在，生产客户端接线仍在进行。
- `designed`: 设计已确认，尚未形成可验收实现。
- `deferred`: 明确延期，不属于当前文字版发布范围。
- `needs_verification`: 有历史信息或链接，但尚未从当前代码或权威文档完成交叉确认。

## Evidence Rules

- `verified_code`: 已从当前仓库代码、测试或 Git 历史验证。
- `canonical_doc`: 来自已确认的总体设计或系统设计文档。
- `audit_report`: 来自审计/验收报告，必须结合修复后的代码与提交判断是否仍有效。
- `conversation_record`: 只在尚未回填到权威文档时作为线索，不能单独证明完成。

目录中的 `unknown` 或 `needs_verification` 是诚实状态，不能为了让表格完整而猜测。修复后应更新对应记录，而不是删除历史证据。
