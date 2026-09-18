"""Final Markdown document renderer for the portfolio review workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


def markdown_cell(value: object) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _review_content(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if content is not None:
        return str(content).strip()
    review_path = item.get("review_path")
    if review_path:
        path = Path(str(review_path))
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return "个股复盘内容缺失。"


def _holding_operation(holding: Mapping[str, Any]) -> str:
    operation = holding.get("operation")
    if not isinstance(operation, dict):
        return "未操作"
    return "{action} {quantity}股 @ {price}".format(
        action=operation.get("action", "待核实"),
        quantity=operation.get("quantity", "待核实"),
        price=operation.get("price", "待核实"),
    )


def render_review_document(
    review_date: str,
    holdings: Sequence[Mapping[str, Any]],
    stock_reviews: Sequence[Mapping[str, Any]],
    summary: str = "",
    failures: Sequence[Mapping[str, Any]] = (),
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render the final portfolio document, including partial failures.

    ``stock_reviews`` items accept either ``content`` or ``review_path``. This
    makes the renderer useful both immediately after model generation and when
    resumed from persisted state.
    """

    metadata = metadata or {}
    year, month, day = review_date.split("-")
    lines: List[str] = [
        f"# {year}.{int(month)}.{int(day)} 持股个股复盘",
        "",
        f"- 请求复盘日期：{review_date}",
        f"- 工作流状态：{metadata.get('workflow_status', 'unknown')}",
        f"- 复盘引擎：{metadata.get('provider', 'auto')}",
        f"- 引擎版本：{metadata.get('model') or '默认模型'}",
        f"- 动态信息来源：{metadata.get('search_provider', 'auto')}",
        f"- 生成时间：{metadata.get('generated_at', '')}",
        "",
        "## 持仓总览",
        "",
        "| 股票 | 成本 | 持股数 | 计划持有时间 | 当日操作 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for holding in holdings:
        lines.append(
            "| {name} | {cost} | {shares} | {plan} | {operation} |".format(
                name=markdown_cell(holding.get("name", "")),
                cost=markdown_cell(holding.get("cost", "")),
                shares=markdown_cell(holding.get("shares", "")),
                plan=markdown_cell(holding.get("plan", "")),
                operation=markdown_cell(_holding_operation(holding)),
            )
        )

    lines.extend(["", "## 执行状态", ""])
    successful = list(stock_reviews)
    failed = list(failures)
    lines.append(
        f"- 成功生成个股复盘：{len(successful)} / {len(holdings)}"
    )
    lines.append(f"- 失败或待重试：{len(failed)}")
    if failed:
        lines.extend(
            [
                "",
                "| 股票 | 失败阶段 | 错误 |",
                "| --- | --- | --- |",
            ]
        )
        for failure in failed:
            lines.append(
                "| {stock} | {stage} | {error} |".format(
                    stock=markdown_cell(failure.get("stock", "")),
                    stage=markdown_cell(failure.get("stage", "")),
                    error=markdown_cell(failure.get("error", "待核实")),
                )
            )

    lines.extend(["", "## 组合级复盘摘要", ""])
    lines.append(summary.strip() or "未生成组合级摘要；请结合下方个股复盘与失败原因人工判断。")

    lines.extend(["", "## 个股完整复盘", ""])
    if not successful:
        lines.append("本轮没有成功生成的个股复盘。截图、状态与失败原因已保留，可在配额恢复后从断点重试。")
    for item in successful:
        stock = item.get("stock", {})
        holding = item.get("holding", {})
        title = "{name} / {code}".format(
            name=stock.get("name", holding.get("name", "未知股票")),
            code=stock.get("code", ""),
        )
        lines.extend([f"### {title}", "", _review_content(item), ""])

    artifact_root = metadata.get("artifact_root")
    if artifact_root:
        lines.extend(
            [
                "## 审计材料",
                "",
                f"- 截图与状态目录：`{artifact_root}`",
                f"- 工作流状态文件：`{metadata.get('state_path', '')}`",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"
