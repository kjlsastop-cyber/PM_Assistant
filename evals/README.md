# PM Assistant Controlled Eval

这是一套 24 题、带人工标准答案的可重复对照评测。它使用一个脱敏的固定项目语料，避免把现有私有知识库内容复制进结果或因知识库更新导致基线漂移。

## 四个实验组

1. `rag`：生产 `kb.py` 混合检索，Reviewer 关闭。
2. `rag_reviewer`：同样检索，Reviewer 开启。
3. `rag_pm`：检索上下文加结构化 Project Memory，Reviewer 关闭。
4. `rag_pm_reviewer`：检索、Project Memory、Reviewer 全部开启。

测试覆盖 8 个静态知识问题、8 个当前项目状态问题、4 个新旧冲突问题、4 个资料不足/抗幻觉问题。人工标准包含理想答案、必含事实、禁止错误和预期来源。

## 运行

```powershell
python evals/run_eval.py --rebuild-index
```

先用两题做冒烟测试：

```powershell
python evals/run_eval.py --limit 2 --rebuild-index
```

中断后续跑：

```powershell
python evals/run_eval.py --resume
```

结果写入 `evals/results/raw.jsonl`、`summary.json` 和 `summary.csv`。完整运行包含 96 次主回答、48 次 Reviewer、96 次裁判调用，以及失败答案的重生成；请先确认模型额度。为减少同模型自评偏差，可配置 `JUDGE_OPENAI_API_KEY`、`JUDGE_OPENAI_BASE_URL`、`JUDGE_MODEL_NAME` 使用另一个裁判模型。

## 解读原则

- 主结论看四组的平均分、正确率与延迟，而不是单个案例。
- 重点分别查看 `project_state`、`conflict` 与 `insufficient` 分类；Project Memory 的价值应主要出现在前两类。
- Reviewer 若只提高文字完整性却不提高正确率，不能宣称质量提升。
- 正式对外发布数据前，应人工复核全部 24×4 条答案及裁判理由，并记录人工与模型裁判的一致率。
