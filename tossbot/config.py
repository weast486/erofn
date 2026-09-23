"""환경변수(.env) 기반 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9), "KST")


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """아주 단순한 .env 로더 (이미 설정된 환경변수는 덮어쓰지 않음)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _get(name: str, default: str) -> str:
    value = os.environ.get(name, "")
    return value if value != "" else default


@dataclass
class Config:
    client_id: str = ""
    client_secret: str = ""
    account_seq: int | None = None
    base_url: str = "https://openapi.tossinvest.com"

    dry_run: bool = True

    total_budget: int = 1_000_000
    num_stocks: int = 10
    stop_loss_pct: float = 4.5
    take_profit_pct: float = 15.0

    # 손절/익절을 토스증권 조건주문으로 서버에 걸어둘지 (false 면 봇의 가격 감시만 사용)
    use_conditional_orders: bool = True

    buy_delay_minutes: int = 10
    liquidation_time: str = "15:10"  # 금요일·공휴일 전날 전량 매도 시각 (KST)
    monitor_interval_seconds: int = 60

    universe_file: str = "universe.txt"
    min_avg_traded_value: float = 3_000_000_000
    max_5d_return_pct: float = 15.0

    state_dir: str = "state"
    log_dir: str = "logs"

    @property
    def slot_budget(self) -> int:
        """종목당 투자금액 (예: 100만원 / 10종목 = 10만원)."""
        return self.total_budget // self.num_stocks

    @property
    def state_file(self) -> Path:
        name = "state.dry.json" if self.dry_run else "state.json"
        return Path(self.state_dir) / name

    @classmethod
    def from_env(cls) -> "Config":
        seq = os.environ.get("TOSS_ACCOUNT_SEQ", "").strip()
        return cls(
            client_id=os.environ.get("TOSS_CLIENT_ID", ""),
            client_secret=os.environ.get("TOSS_CLIENT_SECRET", ""),
            account_seq=int(seq) if seq else None,
            base_url=_get("TOSS_BASE_URL", cls.base_url),
            dry_run=_bool(os.environ.get("DRY_RUN"), True),
            total_budget=int(_get("TOTAL_BUDGET", str(cls.total_budget))),
            num_stocks=int(_get("NUM_STOCKS", str(cls.num_stocks))),
            stop_loss_pct=float(_get("STOP_LOSS_PCT", str(cls.stop_loss_pct))),
            take_profit_pct=float(_get("TAKE_PROFIT_PCT", str(cls.take_profit_pct))),
            use_conditional_orders=_bool(os.environ.get("USE_CONDITIONAL_ORDERS"), True),
            buy_delay_minutes=int(_get("BUY_DELAY_MINUTES", str(cls.buy_delay_minutes))),
            liquidation_time=_get("LIQUIDATION_TIME", cls.liquidation_time),
            monitor_interval_seconds=int(
                _get("MONITOR_INTERVAL_SECONDS", str(cls.monitor_interval_seconds))
            ),
            universe_file=_get("UNIVERSE_FILE", cls.universe_file),
            min_avg_traded_value=float(
                _get("MIN_AVG_TRADED_VALUE", str(cls.min_avg_traded_value))
            ),
            max_5d_return_pct=float(_get("MAX_5D_RETURN_PCT", str(cls.max_5d_return_pct))),
            state_dir=_get("STATE_DIR", cls.state_dir),
            log_dir=_get("LOG_DIR", cls.log_dir),
        )
