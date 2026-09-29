# tossbot — 토스증권 자동매매 봇 (신고가 돌파)

토스증권 Open API(v1.2: 조건주문·랭킹 포함)로 동작하는 자동매매 시스템입니다.
기본 전략은 **20일 신고가 돌파**이고, **신고가 + RSI 평균회귀를 한 계좌로 운용**(`STRATEGY=combo`, 추천)하거나 눌림목 전략(`STRATEGY=pullback`)을 선택할 수 있습니다.

### 신고가 + RSI 한 계좌 운용 (`STRATEGY=combo`)
| 항목 | 규칙 |
|---|---|
| 자금 | 신고가·RSI 가 `TOTAL_BUDGET` 과 최대 `NUM_STOCKS` 종목을 함께 사용 (종목당 금액·1주 가격 상한은 신고가와 같음) |
| 매수 (15:10~15:20, 하루 한 번) | **신고가 후보를 먼저** 사고, 남는 자리를 **RSI 과매도 후보**로 채움 |
| RSI 매수 대상 | 코스피 시가총액 상위 100 (`tossbot/lists/kospi_top100.txt`) 중 **RSI(14) < 30**, RSI 낮은 순 |
| RSI 종목 매도 | 15:10~15:20 에 **RSI ≥ 50** 이면 시장가 / **20거래일째** 시장가 / **손절 -10%** 조건주문 (익절 조건주문 없음) |
| 신고가 종목 매도 | 기존과 같음 (손절 -4.7% / 익절 +20% 조건주문) |

백테스트 (100만원, 종목당 10만원, 비용 포함): 2023 **+29.7%**, 2024 **+21.2%**, 2025 **+43.1%**, 2026(~9/23) **+30.1%**, 2023-01~2026-09 이어서 **+118.3%** (최대 낙폭 -11.0%). 신고가 단독은 +94.3% (-13.1%).
대상 목록 갱신(한두 달에 한 번 권장): `python -m tossbot.backtest universe` (백테스트 데이터 필요).

| 항목 | 규칙 (기본 전략) |
|---|---|
| 매수 대상 | 종가 기준 **20일 신고가**이면서 직전 20거래일 동안 신고가가 없던 **한 달 내 첫 신고가** |
| 거래대금 | 당일 거래대금 **200억원 이상** (직전 20일 평균 30억원 이상) |
| 제외 | **1주 10만원 초과**, 상한가(+29.5% 이상) 마감, **장중 상한가를 찍고 내려온 종목** (`SKIP_TOUCHED_LIMIT_UP`), 우선주·스팩·리츠, 거래정지 |
| 매수 시점 | 매 거래일 **15:10~15:20** (종가 무렵), 하루 한 번. 여러 종목이면 당일 거래대금 큰 순 |
| 자금 | 동시 보유 최대 10종목, 종목당 **봇 평가금액의 10%** (`POSITION_PCT`). 봇 평가금액 = `TOTAL_BUDGET` + 봇 매매 실현손익 + 보유 평가손익 |
| 손절 | 평균 체결가 대비 **-4.7%** 도달 시 **시장가** 매도 (토스 조건주문) |
| 익절 | 평균 체결가 대비 **+20%** 도달 시 **지정가** 매도 (토스 조건주문) |
| 보유 기간 | 제한 없음 (손절·익절이 걸릴 때까지) |
| 시장 필터 | 없음 (`MARKET_FILTER=none`) |

**백테스트 성과** (현재 기본 설정, 100만원 시작, 비용 포함)
- 연도마다 새로 시작 (종목당 10만원 고정): 2023 **+28.8%**, 2024 **+4.7%**, 2025 **+57.2%**, 2026(~9/23) **+7.6%**
- 2023-01 ~ 2026-09 이어서 운용, 종목당 평가금액의 10%: **+129.2%**, 최대 낙폭 -27.1% (고정 10만원이면 +94.3%, -13.1%)
- 효과가 없어 넣지 않은 규칙: 코스피 하락일 매수 금지, 1주 1만원 하한, 시가총액·이평선 배열·갭 필터, 본전 손절·수익 보존 등 (`reports/전략_변경이력.md`)
전체 매매내역과 분석은 [`reports/breakout_20d_sl47_tp20/`](reports/breakout_20d_sl47_tp20/)에 있습니다.

> ⚠️ 이 봇은 투자 수익을 보장하지 않습니다. 과거 성과는 미래 수익을 뜻하지 않습니다. 반드시 `DRY_RUN=true`(기본값)로 충분히 검증한 뒤 실거래로 전환하세요.

## 동작 방식

### 1. 매수 (`tossbot/breakout.py`)
15:10에 후보를 찾습니다.
- **후보군**: 시장 거래대금 상위 100위(실시간) + 당일 상승률 상위 100위(랭킹 API, 투자유의 종목 제외) 중 거래대금 200억 이상, 가격 10만원 이하
- 각 후보의 일봉 60개로 **20일 신고가 / 한 달 내 첫 신고가 / 상한가 여부 / 20일 평균 거래대금**을 판정합니다. 오늘 가격은 15:10 현재가를 씁니다.
- 판정 함수(`breakout.evaluate`)는 백테스트와 같은 코드(`new_high_flags`)를 사용합니다.
- 호가 단위에 맞춘 **시장성 지정가**(현재가 +0.5%)로 주문합니다. 수량은 `floor(100,000 / 지정가)`입니다.

### 2. 손절·익절: 토스증권 조건주문 (`/api/v1/conditional-orders`)
매수가 체결되면 **평균 체결가를 기준으로** 조건주문 2건을 증권사 서버에 등록합니다.
서버가 가격을 감시하므로 봇이 꺼져 있어도, 다음 날 시초가에 갭하락해도 작동합니다.

| 조건주문 | 감시가 (10,800원 매수 시) | 발동 시 주문 |
|---|---|---|
| 손절 (SINGLE) | 진입가 × 0.953, 호가 단위로 내림 (10,290원) | **시장가** 매도 |
| 익절 (SINGLE) | 진입가 × 1.20, 호가 단위로 올림 (12,960원) | 같은 가격에 **지정가** 매도 |

- 토스 OCO는 두 조건 모두 지정가만 지원합니다. 그래서 "손절 시장가 + 익절 지정가"를 SINGLE 2건으로 나눴습니다. **한쪽이 발동되면 봇이 반대쪽을 취소합니다.**
- 조건주문 만료일은 등록일 + 30일입니다(`CONDITIONAL_EXPIRE_DAYS`). 만료되면 봇이 자동으로 다시 겁니다.
- **봇 가격 감시(60초)는 백업**입니다. 조건주문 등록에 실패한 쪽만 봇이 직접 처리합니다.

### 3. 안전장치
- 봇은 **자신이 매수한 종목만** `state/state.json`에 기록해 관리합니다. 사용자가 직접 보유한 종목은 건드리지 않습니다.
- 상태는 파일에 저장되므로 재시작해도 이어서 동작합니다. 실거래(`state.json`)와 모의거래(`state.dry.json`) 상태는 분리합니다.
- 모든 주문에 `clientOrderId`(멱등성 키)를 붙여, 재시도해도 중복 주문이 나가지 않습니다.

## 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env      # TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 입력
```

**API 키 발급**: 토스증권 WTS 로그인 → 설정 → Open API에서 client_id/secret을 발급하고,
**봇을 실행할 PC/서버의 공인 IP를 허용 목록에 등록**하세요.

```bash
python -m tossbot config     # .env 를 반영한 실제 적용 설정 확인 (API 키는 가림)
python -m tossbot check      # 연결·계좌·매수가능금액·오늘 개장 여부 확인
python -m tossbot select     # 지금 기준 매수 후보 (주문 없음, 15시 무렵 실행)
python -m tossbot run        # 자동매매 상주 실행 (DRY_RUN=true 면 모의 체결)
python -m tossbot status     # 보유 종목·손절/익절가·매매 이력·평균 수익률
python -m tossbot liquidate  # 비상시 조건주문 취소 후 봇 보유 종목 전량 시장가 매도
```

**Windows 바로가기**: `windows\make_shortcuts.bat`를 한 번 더블클릭하면 바탕화면에 "토스봇 실행 / 상태확인 / 설정확인" 바로가기가 생깁니다. "토스봇 실행"은 `git pull`로 최신 코드를 받은 뒤 봇을 켭니다(`.venv`가 있으면 그 파이썬 사용).

`run`은 계속 실행되는 프로세스입니다. 서버에서는 `nohup`, `tmux`, systemd 등으로 띄워 두세요.
실거래 전환은 `.env`에서 `DRY_RUN=false`로 바꾸면 됩니다.

## 설정 (`.env`)
값을 바꾼 뒤 봇을 재시작하면 적용됩니다. 손절·익절 %를 바꿔도 이미 걸려 있는 조건주문은 예전 가격 그대로이고, 새로 매수하는 종목부터 적용됩니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `STRATEGY` | breakout | `combo`: 신고가 + RSI 한 계좌 / `breakout`: 신고가 돌파 / `pullback`: 급등 후 이평선 터치 눌림목 |
| `RSI_BUY` / `RSI_SELL` | 30 / 50 | combo: RSI(14) 가 이 값 아래면 매수 / 이 값 이상이면 매도 |
| `RSI_STOP_LOSS_PCT` / `RSI_MAX_HOLD_DAYS` | 10 / 20 | combo: RSI 종목 손절 % / 최대 보유 거래일 |
| `TOTAL_BUDGET` / `NUM_STOCKS` | 1000000 / 10 | 시작 투자금 / 동시 보유 최대 종목 수 |
| `POSITION_PCT` | 10 | 종목당 매수 금액 = 봇 평가금액의 N% (0 = `TOTAL_BUDGET / NUM_STOCKS` 고정) |
| `SKIP_TOUCHED_LIMIT_UP` | true | 당일 장중 고가가 전일 종가 +29.5% 이상이었던 종목 제외 |
| `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` | 4.7 / 20 | 손절 / 익절 % |
| `USE_CONDITIONAL_ORDERS` | true | 손절·익절을 토스 조건주문으로 서버에 등록 |
| `MAX_HOLD_DAYS` | 0 | 최대 보유 거래일 (0 = 제한 없음) |
| `BUY_START` / `BUY_END` | 15:10 / 15:20 | 매수 시간대 |
| `BREAKOUT_ENTRY_DAYS` | 20 | N일 신고가 (종가 기준) |
| `BREAKOUT_FIRST_IN_DAYS` | 20 | 직전 N거래일 안에 신고가가 없던 첫 신고가만 |
| `MIN_DAY_AMOUNT` | 20000000000 | 당일 거래대금 하한 (200억) |
| `MIN_PRICE` / `MAX_PRICE` | 0 / 0 | 1주 가격 하한 / 상한 (하한 0 = 없음, 상한 0 = `TOTAL_BUDGET / NUM_STOCKS` = 10만원까지) |
| `MIN_AVG_TRADING_AMOUNT` | 3000000000 | 직전 20일 평균 거래대금 하한 (30억) |
| `MARKET_FILTER` | none | `kospi_not_down`: 코스피가 전일 종가보다 낮으면 매수 금지 / `none`: 필터 없음 / `kospi_down`: 코스피가 낮을 때만 매수 |

눌림목 전략(`STRATEGY=pullback`)의 설정(`SURGE_PCT`, `MA_PERIOD` 등)은 `.env.example`을 참고하세요.

## 백테스트 (`tossbot/backtest.py`)

```bash
# 한 번에 준비: bash scripts/setup_backtest_data.sh  (data/ohlcv_long + data/marcap/data)
pip install -r requirements-backtest.txt
# 데이터: FinanceData/marcap (KRX 전 종목 일별, 상장폐지 포함) 에서 필요한 연도 파일만 받기
git clone --depth 1 --filter=blob:none --no-checkout https://github.com/FinanceData/marcap
git -C marcap checkout HEAD -- data/marcap-2023.parquet data/marcap-2024.parquet data/marcap-2025.parquet data/marcap-2026.parquet
python -m tossbot.backtest download --source marcap --marcap-dir marcap/data --start 2023-09-01

# 현재 기본 전략 (20일 첫 신고가 · 200억 · 손절 -4.7% / 익절 +20%)
python -m tossbot.backtest run --strategy breakout --stop-loss 4.7 --take-profit 20 --no-exit-on-low \
    --first-in-days 20 --min-day-amount 2e10 --kospi-down none
```

- 연도마다 100만원으로 새로 시작합니다. 연말에 남은 종목은 종가로 평가합니다.
- 비용: 수수료 0.015%(매수·매도), 매도 시 증권거래세(2024 0.18% / 2025 0.15% / 2026 0.20%로 가정), 시장가 체결 슬리피지 0.1%.
- 주요 옵션: `--entry-days`, `--first-in-days`, `--min-day-amount`, `--stop-loss`, `--take-profit`, `--rank-by {amount,change,strength}`, `--entry-delay 3 --delay-max-rise 5 --delay-hold-open --delay-intraday`(3일 뒤 매수), `--kospi-up-days 5`(코스피 5일 상승일만), `--min-price 10000` · `--max-price`(1주 가격 하한·상한), `--max-day-amount`(거래대금 상한), `--max-gap`(시가 갭 상한), `--kospi-regime`(코스피 200일선·20일선 국면).
- 다른 전략: `--strategy pullback`(눌림목), `limitup`(상한가 다음 날 시초가), `surgedoji`(급등 후 단봉 음봉).

**일봉 백테스트의 가정** (실제 결과와 차이가 날 수 있음)
- 신고가 판정과 매수 체결은 **그날 종가** 기준입니다. 실전은 15:10~15:20 현재가로 판단합니다.
- 같은 날 손절가와 익절가에 모두 닿으면 **손절로 가정**합니다(보수적). 갭하락·갭상승이면 시가에 체결됩니다.
- 상한가로 마감한 종목은 종가에 살 수 없으므로 매수에서 제외합니다.
- 실전 봇은 랭킹 상위(거래대금·상승률 각 100위)에서 후보를 찾지만, 백테스트는 전 종목을 봅니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

## 유의사항
- **승률이 20~30%인 전략입니다.** 10번 중 7~8번은 손절됩니다. 연속 손절 구간을 견디는 것이 중요합니다.
- 수익은 해마다 크게 다릅니다(2024년 약 본전, 2025년 +54%). 2024·2026년은 상위 5건을 빼면 손실이었습니다.
- 손절은 시장가라 확실히 팔리지만, 갭하락하면 -4.7%보다 낮은 가격에 체결될 수 있습니다.
- 조건주문 2건이 같은 수량에 동시에 걸리는지는 공개 문서로 확인되지 않았습니다. 실거래 첫날 `python -m tossbot status`로 손절·익절이 "조건주문"으로 표시되는지 확인하세요.
- Open API의 초당 호출 한도는 공개되지 않았습니다. 429 응답을 받으면 `Retry-After`에 맞춰 자동으로 재시도합니다.
