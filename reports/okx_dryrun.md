# OKX FVG 봇 드라이런 시범 운용 (100달러)

- 시작: 2026-10-28 (미국 동부 날짜). 규칙은 바이낸스 FVG 봇과 같음 (전날 거래대금 상위 10, 첫 5분봉 + 1분봉 FVG,
  하루 최대 2종목, 종목당 평가금액 x 5배, 2R 익절, 15:20 정리)
- 평가금액 100 USDT 고정 (드라이런은 손익을 다음 거래 금액에 더하지 않음 → 매 거래 약 500달러어치)
- 체결 가정: 진입가·손절가·익절가 그대로, 시간 정리는 그때 가격. 손익에 수수료·미끄러짐 없음
  (아래 요약에 OKX 수수료 추정 0.05% x 왕복을 따로 뺌)
- 원본: `state_okx\day_날짜.json`, `state_okx\trades.csv`, `state_okx\bot.log`
- 다시 만들기: `python scripts/okx_dryrun_report.py`

## 날마다

| 날짜(동부) | 대상 상위 10 | FVG 신호 | 거래 | 결과 | 그날 손익(USDT) | 누적(USDT) |
|---|---|---|---|---|---|---|
| 2026-11-02 | SNDK, SOXL, INTC, SPCX, MSTR, MU, SNXX, SKHYNIX, SKHY, CRCL | SPCX 롱 09:42, CRCL 롱 09:47 | CRCL 롱 73.52→72.96<br>SPCX 롱 139.95→138.28 | 시간 정리 -0.76%<br>시간 정리 -1.19% | -9.77 | -9.77 |
| 2026-11-03 | SNDK, SOXL, MU, SPCX, SKHYNIX, INTC, SNXX, LITE, CRCL, SKHY | SPCX 롱 09:37, CRCL 롱 09:39 | SPCX 롱 140.25→141.5<br>CRCL 롱 74.06→76.66 | 시간 정리 +0.89%<br>익절 +3.51% | +21.87 | +12.10 |
| 2026-11-04 | SNDK, SOXL, SPCX, MU, SKHYNIX, CRCL, INTC, SNXX, MSTR, SKHY | INTC 롱 09:39, SOXL 롱 09:41, SPCX 롱 09:41, MU 롱 09:41, SKHYNIX 롱 09:41, SKHY 롱 09:41, SNXX 롱 09:42, SNDK 롱 09:44 | SNDK 롱 1108.7→1068.6<br>SKHY 롱 148.19→145.43 | 손절 -3.62%<br>손절 -1.86% | -27.35 | -15.25 |
| 2026-11-05 | SNDK, SOXL, SPCX, MU, SKHYNIX, CRCL, INTC, GOOGL, MSTR, SNXX | INTC 숏 09:37, GOOGL 숏 09:37, MSTR 숏 09:37, SNDK 숏 09:38, MU 숏 09:38, SNXX 숏 09:38, SPCX 숏 09:39, CRCL 숏 09:40, SOXL 숏 09:47, SKHYNIX 숏 09:52 | 없음 | 제외: INTC, SNDK, MU, SNXX, CRCL, SOXL, SKHYNIX | +0.00 | -15.25 |
| 2026-11-06 | SNDK, SOXL, SNXX, MU, SKHYNIX, SPCX, KORU, QQQ, SKHY, INTC | INTC 롱 09:40, QQQ 롱 09:41, SPCX 롱 09:42 | QQQ 롱 661.56→657.11<br>SPCX 롱 132.22→134.19 | 손절 -0.67%<br>시간 정리 +1.49% | +4.11 | -11.14 |

## 요약

- 운용 거래일 5일, 거래 8번 (이익 3 / 손실 5)
- 누적 손익 -11.14 USDT (-11.1%), 수수료 추정 3.99 USDT 빼면 -15.13 USDT (-15.1%)
- 손절 거리 평균 2.08% (5배면 손절 한 번에 평가금액의 약 10.4%)
