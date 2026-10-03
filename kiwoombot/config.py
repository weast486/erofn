"""키움 단타 봇 설정 (.env.kiwoom). 토스 봇의 .env 와 섞지 않는다."""
from __future__ import annotations

import os
from dataclasses import dataclass

from tossbot.config import _bool, _get


@dataclass
class KiwoomConfig:
    app_key: str = ""
    secret_key: str = ""
    mock: bool = True  # 모의투자 서버 (처음엔 반드시 true)
    dry_run: bool = True  # true 면 주문을 보내지 않고 로그만
    # 대상: 전일(기준봉) 종가 상승률·직전 20일 평균 거래대금·1주 가격
    min_prev_change: float = 20.0
    min_avg_amount: float = 3e9
    max_price: float = 100_000
    # 매수: 시가가 전일 종가 아래에서 시작해 전일 종가를 넘으면, buy_until 까지만
    buy_until: str = "09:05"
    position_pct: float = 33.0  # 종목당 아침 평가금액의 N%
    max_positions: int = 3  # 하루 최대 종목 수 (먼저 돌파한 순)
    max_chase_pct: float = 1.5  # 현재가가 전일 종가보다 이 % 넘게 올라 있으면 추격하지 않음
    entry_slip_pct: float = 0.5  # 매수 지정가 = 현재가 + 이 % (전일 종가 +max_chase_pct 를 넘지 않게)
    fill_timeout_sec: int = 20  # 매수 지정가가 이 시간 안에 다 안 채워지면 잔량 취소
    # 매도
    stop_pct: float = 10.0  # 손절 = 매수가 -N% (현재가 감시 → 시장가)
    take_profit_pct: float = 7.0  # 익절 = 매수가 +N% 지정가 (매수 체결 직후 주문)
    exit_time: str = "12:00"  # 이 시각에 남은 주문 취소·보유 전량 시장가 매도
    poll_seconds: float = 2.0
    # 토스 스윙 봇이 들고 있는 종목은 건너뜀 (그 봇의 state.json 경로, 비우면 확인 안 함)
    swing_state_file: str = "state/state.json"
    state_dir: str = "state_kiwoom"
    # ETF 오버나이트: 오늘 ETF 가 전일 종가보다 내렸으면 장 마감 동시호가에 사서 다음 날 장 시작 동시호가에 팖
    etf_enabled: bool = True
    etf_code: str = "229200"  # KODEX 코스닥150 (레버리지는 233740)
    etf_pct: float = 100.0  # 평가금액의 N% (주문가능금액의 98% 안에서)
    etf_max_change: float = 0.0  # 오늘 등락률이 이 % 미만일 때만 매수 (0 = 내린 날)
    etf_buy_time: str = "15:21"  # 장 마감 동시호가(15:20~15:30) 중 시장가 주문
    etf_sell_time: str = "08:45"  # 장 시작 동시호가(08:30~09:00) 중 시장가 주문
    # 수익 적립금: 수익 난 날 하루 손익의 N% 를 주문 금액에서 빼 둠 (0 = 끔, 기본). 운용금이 floor 아래면 적립금에서 채움
    reserve_pct: float = 0.0
    reserve_floor: float = 1_000_000

    @classmethod
    def from_env(cls) -> "KiwoomConfig":
        def f(name: str, attr: str, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            app_key=os.environ.get("KIWOOM_APP_KEY", ""),
            secret_key=os.environ.get("KIWOOM_SECRET_KEY", ""),
            mock=_bool(os.environ.get("KIWOOM_MOCK"), True),
            dry_run=_bool(os.environ.get("KIWOOM_DRY_RUN"), True),
            min_prev_change=f("KW_MIN_PREV_CHANGE", "min_prev_change"),
            min_avg_amount=f("KW_MIN_AVG_AMOUNT", "min_avg_amount"),
            max_price=f("KW_MAX_PRICE", "max_price"),
            buy_until=_get("KW_BUY_UNTIL", cls.buy_until),
            position_pct=f("KW_POSITION_PCT", "position_pct"),
            max_positions=f("KW_MAX_POSITIONS", "max_positions", int),
            max_chase_pct=f("KW_MAX_CHASE_PCT", "max_chase_pct"),
            entry_slip_pct=f("KW_ENTRY_SLIP_PCT", "entry_slip_pct"),
            fill_timeout_sec=f("KW_FILL_TIMEOUT_SEC", "fill_timeout_sec", int),
            stop_pct=f("KW_STOP_PCT", "stop_pct"),
            take_profit_pct=f("KW_TAKE_PROFIT_PCT", "take_profit_pct"),
            exit_time=_get("KW_EXIT_TIME", cls.exit_time),
            poll_seconds=f("KW_POLL_SECONDS", "poll_seconds"),
            swing_state_file=_get("KW_SWING_STATE_FILE", cls.swing_state_file),
            state_dir=_get("KW_STATE_DIR", cls.state_dir),
            etf_enabled=_bool(os.environ.get("KW_ETF_ENABLED"), cls.etf_enabled),
            etf_code=_get("KW_ETF_CODE", cls.etf_code),
            etf_pct=f("KW_ETF_PCT", "etf_pct"),
            etf_max_change=f("KW_ETF_MAX_CHANGE", "etf_max_change"),
            etf_buy_time=_get("KW_ETF_BUY_TIME", cls.etf_buy_time),
            etf_sell_time=_get("KW_ETF_SELL_TIME", cls.etf_sell_time),
            reserve_pct=f("KW_RESERVE_PCT", "reserve_pct"),
            reserve_floor=f("KW_RESERVE_FLOOR", "reserve_floor"),
        )
