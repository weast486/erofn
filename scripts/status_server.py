"""세 봇 매매현황을 이 PC 에서 직접 보여 주는 작은 웹 서버.

    python scripts/status_server.py [--port 8765] [--lan] [--no-open]

브라우저에서 http://localhost:8765 를 열면 그 순간 봇 기록을 읽어 페이지를 새로 만든다.
페이지의 '지금 업데이트' 버튼·새로고침(F5)·자동 갱신(60초)마다 다시 만든다 (Claude 를 거치지 않음).
표의 종목 이름을 누르면 봉 차트에 진입·손절·익절·청산 지점을 보여 준다 (trade_chart.py, 세 봇 모두).
토스 현재가는 최대 60초에 한 번만 다시 조회한다 (가격 조회만, 주문 없음).
기본은 이 PC 에서만 열린다. --lan 을 주면 같은 와이파이의 다른 기기에서도 열린다 (비밀번호 없음 — 집 안에서만).
이미 켜져 있으면 브라우저만 열고 끝난다.
"""
from __future__ import annotations

import argparse
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_status_page  # noqa: E402
import trade_chart  # noqa: E402

PRICE_EVERY = 60  # 토스 현재가를 다시 조회하는 최소 간격(초)
AUTO_REFRESH = 60  # 페이지 자동 갱신 간격(초)
_lock = threading.Lock()
_last_price = [0.0, ""]  # [마지막 조회 시각, 결과 안내]


def refresh_prices() -> str:
    """토스 보유 종목 현재가를 다시 조회해 state/매매현황.json 을 고친다. 안내 문구를 돌려줌."""
    now = time.time()
    if now - _last_price[0] < PRICE_EVERY:
        return _last_price[1]
    note = ""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_trades.py")], cwd=str(ROOT),
                           capture_output=True, timeout=40)
        if r.returncode != 0:
            note = "토스 현재가 조회 실패 — 마지막으로 조회한 가격으로 표시"
    except Exception:  # noqa: BLE001
        note = "토스 현재가 조회 실패 — 마지막으로 조회한 가격으로 표시"
    _last_price[0], _last_price[1] = now, note
    return note


def render() -> bytes:
    with _lock:
        note = refresh_prices()
        toolbar = (
            '<div class="bar"><button type="button" id="refresh">지금 업데이트</button>'
            f'<span>{AUTO_REFRESH}초마다 자동으로 새로 읽습니다.</span>'
            + (f'<span>{note}</span>' if note else '') + '</div>')
        try:
            body = make_status_page.build(toolbar, links=True)
        except Exception as exc:  # noqa: BLE001
            body = (f'<title>봇 매매현황</title><div style="font-family:sans-serif;padding:24px">'
                    f'<h1>페이지를 만들지 못했어요</h1><p>{type(exc).__name__}: {exc}</p>'
                    f'<p>잠시 뒤 새로고침해 보세요.</p></div>')
    script = (
        '<script>(function(){var b=document.getElementById("refresh");'
        'function go(){if(b){b.disabled=true;b.textContent="읽는 중...";}location.reload();}'
        'if(b)b.addEventListener("click",go);'
        f'setTimeout(function(){{if(!document.hidden)go();else document.addEventListener("visibilitychange",go,{{once:true}});}},{AUTO_REFRESH * 1000});'
        '})();</script>')
    page = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{margin:0}</style></head><body>\n' + body + script + '</body></html>\n')
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        m = (re.fullmatch(r"/chart/(binance)/([A-Z0-9]{2,20})/(\d{4}-\d{2}-\d{2})/(day|night)", path)
             or re.fullmatch(r"/chart/(kiwoom)/([0-9A-Z]{6})/(\d{8})", path)
             or re.fullmatch(r"/chart/(toss)/([0-9A-Z]{6})/(\d{4}-\d{2}-\d{2})", path))
        if m:  # 거래 차트: 봉 위에 진입·손절·익절·청산 지점
            data = trade_chart.page(*m.groups()).encode("utf-8")
        elif path in ("/", "/index.html"):
            data = render()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 조용히: 요청마다 한 줄만
        print(f"{datetime.now():%H:%M:%S} {self.client_address[0]} 가 페이지를 열었어요")


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # 윈도우에서는 켜 두면 같은 포트로 두 번 켜져 버림 → 꺼서 '이미 켜져 있음'을 알아챔
    daemon_threads = True


def lan_address() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 실제로 보내지는 않음 — 내 PC 의 집 안 주소만 알아냄
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="세 봇 매매현황 웹 서버 (이 PC)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 다른 기기에서도 열리게 (비밀번호 없음)")
    ap.add_argument("--no-open", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = ap.parse_args()
    url = f"http://localhost:{args.port}"
    try:
        server = Server(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    except OSError:
        print(f"이미 켜져 있어요. 브라우저에서 {url} 을 엽니다.")
        if not args.no_open:
            webbrowser.open(url)
        return
    print(f"봇 매매현황: {url}  (이 창을 닫으면 페이지도 꺼집니다)")
    if args.lan:
        ip = lan_address()
        if ip:
            print(f"같은 와이파이의 휴대폰에서는: http://{ip}:{args.port}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
