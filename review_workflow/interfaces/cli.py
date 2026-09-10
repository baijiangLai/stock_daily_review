"""JSON-friendly command line interface for the review workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from review_workflow.application.service import WorkflowService
from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure.factory import create_workflow_agent
from review_workflow.interfaces.http_api import run_api


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = WorkflowService(create_workflow_agent)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    parser.add_argument("--portfolio", default=str(PROJECT_ROOT / "my_stock.txt"))
    parser.add_argument("--template", default=str(PROJECT_ROOT / "个股复盘模板内容.md"))
    parser.add_argument("--output", help="最终 Markdown 输出路径")
    parser.add_argument("--state-file", help="工作流状态 JSON 路径")
    parser.add_argument("--provider", default="auto", choices=["auto", "gemini", "zhipu"])
    parser.add_argument("--model")
    parser.add_argument(
        "--search-provider",
        default="auto",
        choices=["auto", "zhipu", "model", "none"],
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--device-scale-factor", type=float, default=2.0)
    parser.add_argument("--auth-state")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--skip-capture", action="store_true")
    parser.add_argument("--recapture", action="store_true")
    parser.add_argument("--no-web-search", action="store_true")
    parser.add_argument("--no-peer-capture", action="store_true")
    parser.add_argument("--no-portfolio-summary", action="store_true")
    parser.add_argument("--board", action="append", default=[], metavar="标签:板块代码")
    parser.add_argument("--peer-stock", action="append", default=[], metavar="名称:代码")


def _config(args: argparse.Namespace) -> WorkflowConfig:
    return WorkflowConfig(
        review_date=args.date,
        portfolio_file=Path(args.portfolio).expanduser().resolve(),
        template_file=Path(args.template).expanduser().resolve(),
        output_file=Path(args.output).expanduser().resolve() if args.output else None,
        state_file=Path(args.state_file).expanduser().resolve() if args.state_file else None,
        provider=args.provider,
        model=args.model,
        search_provider=args.search_provider,
        timeout=args.timeout,
        device_scale_factor=args.device_scale_factor,
        auth_state=Path(args.auth_state).expanduser().resolve() if args.auth_state else None,
        headed=args.headed,
        skip_capture=args.skip_capture,
        recapture=args.recapture,
        no_web_search=args.no_web_search,
        no_peer_capture=args.no_peer_capture,
        no_portfolio_summary=args.no_portfolio_summary,
        board=list(args.board),
        peer_stock=list(args.peer_stock),
    )


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可恢复的持股复盘 Agent Workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="初始化一次工作流")
    _add_common_options(start)
    start.add_argument("--force", action="store_true", help="归档旧状态并重新开始")
    start.add_argument("--execute", action="store_true", help="初始化后直接执行到终态")

    resume = subparsers.add_parser("resume", help="从状态文件断点恢复")
    _add_common_options(resume)
    resume.add_argument("--retry-failed", action="store_true", default=True)
    resume.add_argument("--no-retry-failed", dest="retry_failed", action="store_false")
    resume.add_argument("--execute", action="store_true", help="恢复后直接执行到终态")
    resume.add_argument("--step", action="store_true", help="只执行一个原子步骤")

    status_parser = subparsers.add_parser("status", help="查看工作流状态")
    status_parser.add_argument("--date", required=True)
    status_parser.add_argument("--state-file")

    render = subparsers.add_parser("render", help="只根据当前状态重渲染最终文档")
    _add_common_options(render)

    document = subparsers.add_parser("document", help="输出最终 Markdown")
    document.add_argument("--date", required=True)
    document.add_argument("--state-file")

    serve = subparsers.add_parser("serve", help="启动前后端对接 HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            run_api(args.host, args.port)
            return 0
        if args.command == "status":
            state_file = (
                Path(args.state_file).expanduser().resolve()
                if args.state_file
                else None
            )
            config = WorkflowConfig(review_date=args.date, state_file=state_file)
            _print_json(SERVICE.status(config))
            return 0
        if args.command == "document":
            state_file = (
                Path(args.state_file).expanduser().resolve()
                if args.state_file
                else None
            )
            config = WorkflowConfig(review_date=args.date, state_file=state_file)
            print(SERVICE.document(config), end="")
            return 0

        config = _config(args)
        if args.command == "start":
            payload = SERVICE.start(config, force=args.force)
            if args.execute:
                payload = SERVICE.run(config)
        elif args.command == "resume":
            payload = SERVICE.resume(
                config,
                retry_failed=args.retry_failed,
                execute=False,
            )
            if args.step:
                payload = SERVICE.step(config)
            elif args.execute:
                payload = SERVICE.run(config)
        elif args.command == "render":
            payload = SERVICE.render(config)
        else:  # pragma: no cover
            raise RuntimeError(f"未知命令：{args.command}")

        _print_json(payload)
        return 0
    except Exception as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
