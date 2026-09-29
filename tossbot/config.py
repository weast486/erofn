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
    for line in p.read_text(encoding="utf-8-sig").splitlines():
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

    # 전략: breakout = 20일 신고가 돌파 (종가 무렵 매수), pullback = 급등 후 이평선 터치 눌림목,
    #       combo = 신고가 + RSI 평균회귀를 한 계좌로 (신고가 후보 먼저, 남는 자리를 RSI 후보로)
    strategy: str = "breakout"

    total_budget: int = 1_000_000
    num_stocks: int = 10  # 동시 보유 최대 종목 수 (종목당 = 총액 / 종목 수)
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 20.0

    # 손절/익절을 토스증권 조건주문으로 서버에 걸어둘지 (false 면 봇의 가격 감시만 사용)
    use_conditional_orders: bool = True
    # 조건주문 만료일 (등록일 + N일). 만료되면 봇이 자동으로 다시 건다
    conditional_expire_days: int = 30

    # 매수 가능 시간대 (KST). 15:20 부터는 종가 단일가라 그 전까지만 매수한다
    buy_start: str = "15:10"  # 신고가 전략: 종가 무렵 매수
    buy_end: str = "15:20"
    monitor_interval_seconds: int = 60

    # 눌림목 조건
    surge_pct: float = 10.0  # 급등 기준: 하루 상승률 %
    surge_lookback_days: int = 10  # 최근 N거래일 안의 급등만 인정
    ma_period: int = 7  # 7일선
    ma_max_break_pct: float = 1.5  # 현재가 <= 이평선이면 터치. 단 N% 넘게 아래면 이탈로 보고 매수 안 함
    require_ma_rising: bool = True  # 이평선이 상승 중인 종목만
    min_avg_trading_amount: float = 3_000_000_000  # 20일 평균 거래대금 하한
    rebuy_cooldown_days: int = 5  # 매도한 종목은 N일 동안 다시 사지 않음

    # 신고가 돌파 조건
    breakout_entry_days: int = 20  # N일 신고가 (종가 기준)
    breakout_first_in_days: int = 20  # 직전 N거래일 안에 신고가가 없던 첫 신고가만
    min_day_amount: float = 20_000_000_000  # 당일 거래대금 하한 (200억)
    max_price: float = 0  # 1주 가격 상한 (0 = 종목당 예산까지)
    min_price: float = 0  # 1주 가격 하한 (0 = 없음)
    skip_touched_limit_up: bool = True  # 장중 상한가를 찍고 내려온 종목 제외
    # 0 보다 크면 종목당 매수 금액 = 봇 평가금액(TOTAL_BUDGET + 실현손익 + 보유 평가손익) x N %. 0 이면 TOTAL_BUDGET / NUM_STOCKS 고정
    position_pct: float = 10.0
    # 시장 필터: kospi_down = 코스피가 전일 종가보다 낮을 때만 신규 매수, none = 필터 없음
    market_filter: str = "none"
    min_days_after_surge: int = 3  # 급등일로부터 N거래일째부터 매수 허용
    pullback_volume_ratio: float = 0.5  # 급등 이후 평균 거래량 <= 급등일 거래량 x N
    # RSI 평균회귀 (STRATEGY=combo): 코스피 시가총액 상위 목록 중 RSI < RSI_BUY 면 종가 무렵 매수,
    # RSI >= RSI_SELL 또는 RSI_MAX_HOLD_DAYS 거래일째 종가 무렵 시장가 매도, 손절 -RSI_STOP_LOSS_PCT% 조건주문
    rsi_period: int = 14
    rsi_buy: float = 30.0
    rsi_sell: float = 50.0
    rsi_stop_loss_pct: float = 10.0
    rsi_max_hold_days: int = 20
    rsi_universe_file: str = ""  # 비우면 tossbot/lists/kospi_top100.txt
    # 최대 보유 기간: 매수일로부터 N거래일째 TIME_EXIT_TIME 에 시장가 매도 (0 이면 제한 없음)
    max_hold_days: int = 0
    time_exit_time: str = "15:10"

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

        def num(name: str, attr: str, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            client_id=os.environ.get("TOSS_CLIENT_ID", ""),
            client_secret=os.environ.get("TOSS_CLIENT_SECRET", ""),
            account_seq=int(seq) if seq else None,
            base_url=_get("TOSS_BASE_URL", cls.base_url),
            dry_run=_bool(os.environ.get("DRY_RUN"), True),
            total_budget=num("TOTAL_BUDGET", "total_budget", int),
            num_stocks=num("NUM_STOCKS", "num_stocks", int),
            stop_loss_pct=num("STOP_LOSS_PCT", "stop_loss_pct"),
            take_profit_pct=num("TAKE_PROFIT_PCT", "take_profit_pct"),
            use_conditional_orders=_bool(os.environ.get("USE_CONDITIONAL_ORDERS"), True),
            conditional_expire_days=num("CONDITIONAL_EXPIRE_DAYS", "conditional_expire_days", int),
            buy_start=_get("BUY_START", cls.buy_start),
            buy_end=_get("BUY_END", cls.buy_end),
            monitor_interval_seconds=num("MONITOR_INTERVAL_SECONDS", "monitor_interval_seconds", int),
            surge_pct=num("SURGE_PCT", "surge_pct"),
            surge_lookback_days=num("SURGE_LOOKBACK_DAYS", "surge_lookback_days", int),
            ma_period=num("MA_PERIOD", "ma_period", int),
            ma_max_break_pct=num("MA_MAX_BREAK_PCT", "ma_max_break_pct"),
            require_ma_rising=_bool(os.environ.get("REQUIRE_MA_RISING"), True),
            min_avg_trading_amount=num("MIN_AVG_TRADING_AMOUNT", "min_avg_trading_amount"),
            rebuy_cooldown_days=num("REBUY_COOLDOWN_DAYS", "rebuy_cooldown_days", int),
            strategy=_get("STRATEGY", cls.strategy),
            breakout_entry_days=num("BREAKOUT_ENTRY_DAYS", "breakout_entry_days", int),
            breakout_first_in_days=num("BREAKOUT_FIRST_IN_DAYS", "breakout_first_in_days", int),
            min_day_amount=num("MIN_DAY_AMOUNT", "min_day_amount"),
            max_price=num("MAX_PRICE", "max_price"),
            min_price=num("MIN_PRICE", "min_price"),
            skip_touched_limit_up=_bool(os.environ.get("SKIP_TOUCHED_LIMIT_UP"), cls.skip_touched_limit_up),
            position_pct=num("POSITION_PCT", "position_pct"),
            market_filter=_get("MARKET_FILTER", cls.market_filter),
            min_days_after_surge=num("MIN_DAYS_AFTER_SURGE", "min_days_after_surge", int),
            pullback_volume_ratio=num("PULLBACK_VOLUME_RATIO", "pullback_volume_ratio"),
            max_hold_days=num("MAX_HOLD_DAYS", "max_hold_days", int),
            time_exit_time=_get("TIME_EXIT_TIME", cls.time_exit_time),
            rsi_period=num("RSI_PERIOD", "rsi_period", int),
            rsi_buy=num("RSI_BUY", "rsi_buy"),
            rsi_sell=num("RSI_SELL", "rsi_sell"),
            rsi_stop_loss_pct=num("RSI_STOP_LOSS_PCT", "rsi_stop_loss_pct"),
            rsi_max_hold_days=num("RSI_MAX_HOLD_DAYS", "rsi_max_hold_days", int),
            rsi_universe_file=_get("RSI_UNIVERSE_FILE", cls.rsi_universe_file),
            state_dir=_get("STATE_DIR", cls.state_dir),
            log_dir=_get("LOG_DIR", cls.log_dir),
        )
