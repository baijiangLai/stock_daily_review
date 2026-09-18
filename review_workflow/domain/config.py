"""Configuration model for the review workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class WorkflowConfig:
    review_date: str
    portfolio_file: Path = PROJECT_ROOT / "my_stock.txt"
    template_file: Path = PROJECT_ROOT / "个股复盘模板内容.md"
    output_file: Optional[Path] = None
    state_file: Optional[Path] = None
    provider: str = "auto"
    model: Optional[str] = None
    search_provider: str = "auto"
    timeout: float = 180.0
    device_scale_factor: float = 2.0
    auth_state: Optional[Path] = None
    headed: bool = False
    skip_capture: bool = False
    recapture: bool = False
    no_web_search: bool = False
    no_peer_capture: bool = False
    no_portfolio_summary: bool = False
    board: List[str] = field(default_factory=list)
    peer_stock: List[str] = field(default_factory=list)
    holdings: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "WorkflowConfig":
        return cls(
            review_date=str(value["review_date"]),
            portfolio_file=Path(value.get("portfolio_file", PROJECT_ROOT / "my_stock.txt")),
            template_file=Path(value.get("template_file", PROJECT_ROOT / "个股复盘模板内容.md")),
            output_file=Path(value["output_file"]) if value.get("output_file") else None,
            state_file=Path(value.get("state_file")) if value.get("state_file") else None,
            provider=str(value.get("provider", "auto")),
            model=value.get("model"),
            search_provider=str(value.get("search_provider", "auto")),
            timeout=float(value.get("timeout", 180.0)),
            device_scale_factor=float(value.get("device_scale_factor", 2.0)),
            auth_state=Path(value["auth_state"]) if value.get("auth_state") else None,
            headed=bool(value.get("headed", False)),
            skip_capture=bool(value.get("skip_capture", False)),
            recapture=bool(value.get("recapture", False)),
            no_web_search=bool(value.get("no_web_search", False)),
            no_peer_capture=bool(value.get("no_peer_capture", False)),
            no_portfolio_summary=bool(value.get("no_portfolio_summary", False)),
            board=list(value.get("board", [])),
            peer_stock=list(value.get("peer_stock", [])),
            holdings=list(value.get("holdings", [])),
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["portfolio_file"] = str(self.portfolio_file)
        value["template_file"] = str(self.template_file)
        value["output_file"] = str(self.output_file) if self.output_file else None
        value["state_file"] = str(self.state_file) if self.state_file else None
        value["auth_state"] = str(self.auth_state) if self.auth_state else None
        value["holdings"] = [dict(item) for item in self.holdings]
        return value
