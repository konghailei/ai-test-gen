# 本次架构审查概览

## 完成内容

- 基于实际代码梳理了需求上传、解析、澄清、门禁、影响分析、测试设计、用例生成、审核、知识回流、复用与治理的完整流程。
- 对照 Anthropic、OpenAI、LangGraph、OpenTelemetry、OWASP、NIST、Transactional Outbox 与 mutation testing 等最佳实践完成差距分析。
- 输出正式报告：`docs/15-AI-Agent端到端架构审查与优化方案.md`。

## 核心结论

当前“确定性工作流 + 专门 Agent + 程序化门禁 + 人工确认 + RAG”的主架构方向正确，不建议改造成自由规划式多 Agent。

系统适合内部受控试运行，但尚不适合无人值守自动发布到权威知识库。优先级最高的风险是：

1. Review LLM 降级或 QualityGate 强制放行后，结果仍可能自动 approved 并写入 Milvus；
2. LangGraph 未配置持久 checkpointer，现有续跑字段未完整进入 PipelineState；
3. 生成互斥有 TOCTOU，且全局 Orchestrator 共享请求级事件队列；
4. PG 与 Milvus 非原子双写，TestCase 缺少 outbox 与双向对账；
5. CI 和 Agent eval 未形成真正的发布门禁。

## 建议顺序

1. 先做 fail-closed：降级/强制放行只进入人工审核，不自动发布知识库；
2. 再补 durable execution、并发隔离与 transactional outbox；
3. 随后补强 Structured Outputs、覆盖证据、版本化 eval 与标准 trace；
4. 最后完善 Agent 安全红队、claim-to-source 和 mutation testing。

## 备注

- 报告中的缺陷均区分了代码实证、潜在风险和最佳实践映射。
- 本次只做架构审查与方案输出，未修改业务逻辑。
