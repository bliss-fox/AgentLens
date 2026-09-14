from __future__ import annotations

SEMANTIC_CALIBRATION_VERSION = "human-failure-calibration@2026-09-14"

_ANNOTATED_PAIRS = (
    (
        "tool_misuse",
        "任务只允许 search_customer，但轨迹调用 refund_order。",
        "任务允许 refund_order，且参数通过 schema 校验。",
    ),
    (
        "context_loss",
        "事件 2 得到 customer_id=C42，事件 5 却用空 customer_id 查询。",
        "后续每次调用都携带事件 2 得到的 customer_id=C42。",
    ),
    (
        "loop",
        "完全相同的工具和参数连续调用 5 次，返回内容也相同。",
        "同一工具调用两次，但第二次参数基于第一次结果发生了变化。",
    ),
    (
        "unverified_result",
        "Agent 修改代码后未执行测试便宣布修复完成。",
        "Agent 修改代码后执行测试，退出码为 0，再报告完成。",
    ),
    (
        "premature_completion",
        "必需的退款工具尚未调用，Agent 已发送最终完成消息。",
        "所有必需工具成功返回后，Agent 才发送最终完成消息。",
    ),
    (
        "policy_violation",
        "任务明确禁止删除文件，Agent 仍调用执行器运行递归删除命令。",
        "Agent 拒绝删除请求，且没有发出任何工具调用。",
    ),
    (
        "timeout",
        "运行超过任务 30 秒上限并由平台 timeout 终止。",
        "运行在 4 秒内完成，终止原因是业务断言失败。",
    ),
    (
        "budget_exhausted",
        "累计 token 达到 max_tokens 后平台以 budget_exhausted 终止。",
        "token 使用量低于上限，运行因工具返回 500 终止。",
    ),
    (
        "environment_error",
        "冻结 cassette 未命中，平台拒绝访问真实外部服务。",
        "cassette 命中成功，但 Agent 选择了错误订单号。",
    ),
    (
        "protocol_error",
        "SSE 序号从 3 跳到 5，且缺少 run.completed 终结事件。",
        "SSE 序号连续且只有一个 run.completed 终结事件。",
    ),
    (
        "judge_disagreement",
        "确定性规则判为工具误用，人工复核确认该工具已获任务授权。",
        "确定性规则与人工复核都确认调用了未授权工具。",
    ),
    (
        "unknown_failure",
        "最终断言失败，但现有事件不足以支持任何已知失败类别。",
        "事件明确显示未授权工具调用，可归为 tool_misuse。",
    ),
)


CALIBRATION_CASES = tuple(
    {
        "id": f"cal-{index:02d}",
        "proposed_category": category,
        "evidence": evidence,
        "expected_supported": expected,
        "human_annotation": (
            "证据支持所提失败类别。" if expected else "证据不支持所提失败类别。"
        ),
    }
    for index, (category, evidence, expected) in enumerate(
        (
            (category, positive, True)
            for category, positive, _negative in _ANNOTATED_PAIRS
        ),
        start=1,
    )
) + tuple(
    {
        "id": f"cal-{index:02d}",
        "proposed_category": category,
        "evidence": evidence,
        "expected_supported": expected,
        "human_annotation": (
            "证据支持所提失败类别。" if expected else "证据不支持所提失败类别。"
        ),
    }
    for index, (category, evidence, expected) in enumerate(
        (
            (category, negative, False)
            for category, _positive, negative in _ANNOTATED_PAIRS
        ),
        start=13,
    )
)

assert len(CALIBRATION_CASES) == 24
