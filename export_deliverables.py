"""
export_deliverables.py — 경쟁단지 비교 산출물 로컬 생성 (플랜 jimeok-v3 T11)
==============================================================================
분양성검토 보고서용 경쟁단지 비교 산출물을 로컬에서만 생성한다.

산출물 6종 (--out 폴더):
  지먹_<lawd>_지도_<ts>.png        지도 캡처 (playwright, #map 스크린샷)
  지먹_<lawd>_보드_<ts>.png        보드 (지도 PNG + 통계표 나란히, 스크립트 조립 HTML)
  경쟁단지_<lawd>_<ts>.xlsx        시트1 경쟁단지 비교 · 시트2 메타 · 시트3 타입 상세
  경쟁단지_<lawd>_<ts>_원시거래.csv  window 내 원시 거래 (직거래 플래그 포함)
  지먹_<lawd>_경쟁지도_<ts>.pptx    지도 풀블리드 + 경쟁단지 링/번호 + 출처 (16:9)
  (보드 조립에 쓴 HTML도 참고용으로 함께 남김)

산식 정본 = _디자인기획_지도디테일_v3.md §6 (브라우저 T9 computeCompetitorStats와 동일 기준).
중복 제거는 수집 단계 8필드 UNIQUE(db.py idx_trade_unique_v2)에 이미 내장 — 통계 단계 dedup 없음.

100% 로컬 — 외부 API 호출 0 (타일 이미지는 브라우저가 그리는 지도 배경일 뿐 수집 호출이 아님).
DB는 read-only URI(mode=ro) 연결. 정본 파일(map_leaflet.html 등) 수정 0.

사용법:
  python3 export_deliverables.py --lawd 11590 --selection sel.json --out _output \\
      [--scale 2|3] [--basemap minimal|voyager|dark|sat] [--period 3|6|12|24] \\
      [--project 사업명] [--include-direct]

  --selection  스키마 [{i, name, color, order}] — i는 번들 apts 인덱스.
               파일 미존재 시 사용법 안내 후 정상 종료(크래시 없음).
  --project    지정 시 _projects/<사업명>/.dev/ 에 전 산출물 복사 (폴더 없으면 경고 후 스킵, 신규 생성 금지).

의존: pip3 install playwright (+ chromium: 캐시 ~/Library/Caches/ms-playwright 자동 재사용),
      openpyxl, python-pptx.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import glob
import html as html_mod
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent

PORT = 8794                      # 이 스크립트 전용 로컬 서버 포트 (8793=_deploy 기존, 함정 #10 패턴)
VIEWPORT = {"width": 1440, "height": 900}
CMP_RADIUS_M = 300               # 스펙 §4 경쟁 링 반경
SITE_RADII_M = (500, 1000, 2000) # 사업지 반경원 3개 (site 존재 시에만 — region 번들은 null)
PALETTE = ["#0071E3", "#3182ce", "#38a169", "#d69e2e", "#dd6b20",
           "#e53e3e", "#7c3aed", "#db2777", "#0d9488"]  # 스펙 §3.3 경쟁 9색
PROJECTS_ROOT = Path("/Users/dina/Dropbox/01_활성프로젝트/분양_부동산/0.분양성검토_엔진/_projects")
M_PER_DEG_LAT = 111320.0         # 위도 1도 ≈ 미터 (표준, 스펙 §6.1 haversine R=6371000과 동일 지구 반경 계)

USAGE_EXAMPLE = (
    "사용 예:\n"
    "  python3 export_deliverables.py --lawd 11590 --selection sel.json --out _output\n"
    "  selection JSON 스키마: [{\"i\": 0, \"name\": \"단지명\", \"color\": \"#0071E3\", \"order\": 1}]\n"
    "  (i = 번들 apts 인덱스 · color 생략 시 §3.3 팔레트 순차 배정)"
)


# ─────────────────────────── 공용 유틸 ───────────────────────────

def die(msg: str, code: int = 1) -> None:
    """❌ 메시지 출력 후 종료 (traceback 없음)."""
    print(f"❌ {msg}")
    sys.exit(code)


def round_half_up(x: float, ndigits: int = 0) -> float:
    """JS Math.round와 동일한 half-up 반올림 (Python round는 은행원 반올림이라 다름)."""
    if x != x or x in (float("inf"), float("-inf")):
        return x
    factor = 10 ** ndigits
    return math.floor(x * factor + 0.5) / factor


def fmt1(x) -> str:
    """소수 1자리 표시 (스펙 §6.1 표시 관례: 계산은 원본 정밀도, 표시만 1자리)."""
    if x is None:
        return "—"
    return f"{round_half_up(x, 1):,.1f}"


def fmt_int(x) -> str:
    if x is None:
        return "—"
    return f"{int(x):,}"


def png_size(path: Path) -> tuple[int, int]:
    """PNG IHDR에서 픽셀 크기 읽기 (PIL 의존 없이)."""
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"PNG 아님: {path}")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def subtract_months(date_str: str, months: int) -> str:
    """스펙 §6.1 period_start: 캘린더 개월 차감. 차감한 달에 해당 일이 없으면 그 달의 마지막 날."""
    y, m, d = (int(v) for v in date_str.split("-"))
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    d = min(d, calendar.monthrange(y, m)[1])
    return f"{y:04d}-{m:02d}-{d:02d}"


def load_bundle(lawd: str) -> dict:
    """regions/<lawd>/map_data.js 번들 읽기."""
    path = TOOL_ROOT / "regions" / lawd / "map_data.js"
    if not path.exists():
        die(f"번들 없음: {path} — build_hub.py로 먼저 생성하거나 --lawd 확인")
    txt = path.read_text(encoding="utf-8")
    prefix = "window.MAP_DATA = "
    if not txt.startswith(prefix):
        die(f"번들 형식 이상: {path}")
    try:
        return json.loads(txt[len(prefix):].rstrip(";\n"))
    except json.JSONDecodeError as e:
        die(f"번들 JSON 파싱 실패: {path} — {e}")


def load_selection(path_str: str, apts: list[dict]) -> list[dict]:
    """selection JSON 읽어 검증·정규화. 파일 미존재 = 사용법 안내 후 정상 종료."""
    path = Path(path_str)
    if not path.exists():
        print(f"⚠️  selection 파일이 없습니다: {path}\n")
        print(USAGE_EXAMPLE)
        sys.exit(0)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        die(f"selection JSON 파싱 실패: {path} — {e}")
    if not isinstance(raw, list) or not raw:
        die(f"selection은 비어있지 않은 배열이어야 합니다: {path}\n\n{USAGE_EXAMPLE}")

    valid_i = {a["i"] for a in apts}
    sel = []
    for n, item in enumerate(raw):
        if not isinstance(item, dict) or "i" not in item:
            die(f"selection[{n}] 에 'i'(번들 apts 인덱스)가 없습니다.\n\n{USAGE_EXAMPLE}")
        i = int(item["i"])
        if i not in valid_i:
            die(f"selection[{n}].i={i} 는 번들 apts 인덱스 범위 밖 (0~{max(valid_i)})")
        apt = next(a for a in apts if a["i"] == i)
        order = int(item.get("order", n + 1))
        color = item.get("color") or PALETTE[(order - 1) % len(PALETTE)]
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            print(f"⚠️  selection[{n}].color='{color}' 형식 이상 → 팔레트 기본색으로 대체")
            color = PALETTE[(order - 1) % len(PALETTE)]
        sel.append({"i": i, "name": item.get("name") or apt["name"], "apt": apt,
                    "color": color, "order": order})
    sel.sort(key=lambda s: s["order"])
    return sel


# ─────────────────────────── 통계 (스펙 v3 §6 정본) ───────────────────────────

def open_db_ro(lawd: str) -> sqlite3.Connection:
    """DB read-only 연결 (mode=ro URI — 쓰기 원천 차단)."""
    path = TOOL_ROOT / "data" / f"realprice_{lawd}.db"
    if not path.exists():
        die(f"DB 없음: {path} — collect.py로 먼저 수집하거나 --lawd 확인")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def valid_trade(r: sqlite3.Row) -> bool:
    """평당가 계산 불가 행(면적·평수·금액·날짜 누락) 제외 — Python/SQL 양쪽 동일 필터."""
    return bool(r["deal_date"] and r["deal_amount"] is not None
                and r["exclu_area"] is not None and r["pyeong"])


def compute_stats(conn: sqlite3.Connection, lawd: str, sel: list[dict],
                  period_months: int, exclude_direct: bool) -> dict:
    """스펙 §6 산식 그대로. 반환: {max_date, period_start, window..., apts:[{rows...}]}."""
    rows = conn.execute(
        "SELECT apt_name, legal_dong, exclu_area, pyeong, deal_amount, deal_date, floor, deal_gbn "
        "FROM trades WHERE lawd_cd = ?", (lawd,)).fetchall()
    trades = [r for r in rows if valid_trade(r)]
    skipped = len(rows) - len(trades)
    if skipped:
        print(f"⚠️  필드 누락 거래 {skipped}건 집계 제외 (면적/평수/금액/날짜 없음)")
    if not trades:
        die("trades 테이블에 유효 거래가 0건입니다")

    # §6.1: max_date = 데이터 전체 trades의 최대 거래일 (호가/오늘 아님 — §6.4)
    max_date = max(r["deal_date"] for r in trades)
    base_year = int(max_date[:4])
    period_start = subtract_months(max_date, period_months)

    def is_direct(r: sqlite3.Row) -> bool:
        return r["deal_gbn"] == "직거래"

    def kept(rs):
        """직거래 제외 옵션(§6.6, 기본 ON) 적용."""
        return [r for r in rs if not (exclude_direct and is_direct(r))]

    window_all = kept([r for r in trades if period_start < r["deal_date"] <= max_date])

    def trades_for(name: str, dong: str | None) -> list:
        """단지 매칭 — 동명이단지는 법정동으로 구분 (db.py apt_key rationale)."""
        t = [r for r in trades if r["apt_name"] == name]
        if dong and any(r["legal_dong"] == dong for r in t):
            t = [r for r in t if r["legal_dong"] == dong]
        return t

    def peak(rs):
        """§6.5 최고점: max(amt) + 금액·날짜·전용·층 병기."""
        if not rs:
            return None
        r = max(rs, key=lambda x: x["deal_amount"])
        return {"amt": r["deal_amount"], "date": r["deal_date"],
                "ar": r["exclu_area"], "floor": r["floor"]}

    def type_label(key: int) -> str:
        """§6.2: '<floor>㎡ (≈N평)', N = round(floor 키 × 0.3025)."""
        return f"{key}㎡ (≈{int(round_half_up(key * 0.3025))}평)"

    def row_stats(rs) -> dict:
        """§6.3 평당가 평균+중위 (짝수 표본 = 중앙 2개 평균)."""
        vals = [r["deal_amount"] / r["pyeong"] for r in rs]
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        mid = (vals_sorted[n // 2] if n % 2 == 1
               else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2)
        return {"avg": sum(vals) / n, "median": mid, "count": n}

    apt_out = []
    for s in sel:
        apt = s["apt"]
        mine = trades_for(apt["name"], apt.get("dong"))
        mine_all = kept(mine)                                   # 역대 (기간 무관, §6.5)
        mine_win = [r for r in mine_all
                    if period_start < r["deal_date"] <= max_date]  # window (§6.1)

        # §6.2 타입 그룹: 키 = floor(exclu_area), 표본 ≥3건만 행, 미만은 "기타" 합산
        groups: dict[int, list] = {}
        for r in mine_win:
            groups.setdefault(math.floor(r["exclu_area"]), []).append(r)
        big = {k: v for k, v in groups.items() if len(v) >= 3}
        etc_keys = [k for k, v in groups.items() if len(v) < 3]
        etc_win = [r for k in etc_keys for r in groups[k]]
        etc_all = [r for r in mine_all if math.floor(r["exclu_area"]) in etc_keys]

        def alltime(rs_same_type_set):
            return peak(rs_same_type_set)

        rows_out = []
        for key in sorted(big):
            rs = big[key]
            st = row_stats(rs)
            rows_out.append({"type": type_label(key), "type_key": key, **st,
                             "peak": peak(rs), "alltime": alltime(
                                 [r for r in mine_all if math.floor(r["exclu_area"]) == key])})
        if etc_win:
            st = row_stats(etc_win)
            rows_out.append({"type": "기타", "type_key": None, **st,
                             "peak": peak(etc_win), "alltime": alltime(etc_all)})
        if not rows_out:
            rows_out.append({"type": "—", "type_key": None, "avg": None, "median": None,
                             "count": 0, "peak": None, "alltime": None})

        apt_out.append({**s, "rows": rows_out,
                        "win_total": len(mine_win), "all_total": len(mine_all),
                        "window_vals": [r["deal_amount"] / r["pyeong"] for r in mine_win]})

    return {"max_date": max_date, "base_year": base_year, "period_start": period_start,
            "period_months": period_months, "exclude_direct": exclude_direct,
            "window_total": len(window_all), "apts": apt_out}


def cross_check_sql(conn: sqlite3.Connection, lawd: str, stats: dict) -> bool:
    """독립 2차 검증: Python 집계와 별도 SQL 직접 쿼리 결과 대조 (xlsx↔DB 일치 확인)."""
    ok = True
    md_sql = conn.execute("SELECT MAX(deal_date) FROM trades WHERE lawd_cd = ?",
                          (lawd,)).fetchone()[0]
    if md_sql != stats["max_date"]:
        print(f"❌ max_date 불일치: Python {stats['max_date']} vs SQL {md_sql}")
        ok = False
    else:
        print(f"✅ max_date 일치: {md_sql}")

    direct_sql = " AND (deal_gbn IS NULL OR deal_gbn <> '직거래')" if stats["exclude_direct"] else ""
    for a in stats["apts"]:
        apt = a["apt"]
        # compute_stats의 trades_for()와 동일 매칭: 동명이단지는 법정동으로 구분
        dong_sql, dong_param = "", []
        if apt.get("dong"):
            has_dong = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE lawd_cd=? AND apt_name=? AND legal_dong=?",
                (lawd, apt["name"], apt["dong"])).fetchone()[0]
            if has_dong:
                dong_sql, dong_param = " AND legal_dong = ?", [apt["dong"]]
        q = ("SELECT COUNT(*) c, AVG(deal_amount / pyeong) v, MAX(deal_amount) m "
             "FROM trades WHERE lawd_cd = ? AND apt_name = ? AND deal_date > ? AND deal_date <= ? "
             "AND exclu_area IS NOT NULL AND pyeong > 0 AND deal_amount IS NOT NULL"
             + dong_sql + direct_sql)
        r = conn.execute(q, [lawd, apt["name"], stats["period_start"], stats["max_date"]]
                         + dong_param).fetchone()
        py_count = a["win_total"]
        py_avg = (sum(a["window_vals"]) / len(a["window_vals"])) if a["window_vals"] else None
        # 타입별 peak의 최대 = window 전체 최대 (타입이 window를 분할하므로)
        py_max = max((row["peak"]["amt"] for row in a["rows"] if row["peak"]), default=None)
        same_count = (r["c"] == py_count)
        same_avg = (r["v"] is None and py_avg is None) or \
                   (r["v"] is not None and py_avg is not None and
                    abs(round_half_up(r["v"], 1) - round_half_up(py_avg, 1)) < 1e-9)
        same_max = (r["m"] == py_max)
        good = same_count and same_avg and same_max
        if not good:
            ok = False
        print(f"{'✅' if good else '❌'} {apt['name']}: "
              f"SQL count={r['c']} avg={fmt1(r['v'])} max={fmt_int(r['m'])}"
              f" | Python count={py_count} avg={fmt1(py_avg)} max={fmt_int(py_max)}")
    return ok


# ─────────────────────────── 로컬 서버 (함정 #10: 중복 기동 금지) ───────────────────────────

def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_server() -> subprocess.Popen | None:
    """curl-first: 이미 떠 있으면 재사용, 없을 때만 기동. 반환 Popen은 우리가 기동한 것만."""
    probe = f"http://localhost:{PORT}/map_leaflet.html"
    if _http_ok(probe):
        print(f"✅ 서버 재사용: localhost:{PORT} (이미 떠 있음 — 중복 기동 스킵)")
        return None
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(PORT)], cwd=TOOL_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _http_ok(probe):
            print(f"✅ 서버 기동: localhost:{PORT} (pid {proc.pid})")
            return proc
        time.sleep(0.5)
    proc.kill()
    die(f"서버 기동 실패: localhost:{PORT}")


# ─────────────────────────── 브라우저 캡처 ───────────────────────────

def find_cached_chromium() -> str | None:
    """~/Library/Caches/ms-playwright 의 chromium 캐시 재사용 (다운로드 0 시도)."""
    home = os.path.expanduser("~")
    pats = [f"{home}/Library/Caches/ms-playwright/chromium-*/chrome-mac-arm64/*.app/Contents/MacOS/*",
            f"{home}/Library/Caches/ms-playwright/chromium-*/chrome-linux/chrome"]
    cands = []
    for pat in pats:
        cands += [c for c in glob.glob(pat) if os.access(c, os.X_OK)]
    if not cands:
        return None
    def rev(p: str) -> int:
        m = re.search(r"chromium-(\d+)", p)
        return int(m.group(1)) if m else 0
    return max(cands, key=rev)


def capture_map(lawd: str, sel: list[dict], basemap: str, scale: int, out_png: Path) -> dict | None:
    """정본 로드 → selection 주입 → 베이스맵 설정 → #map 스크린샷 + bounds 기록.

    반환: {n,s,e,w} 바운드 (캡처 시점 map.getBounds()) — PPTX 좌표 변환용. 실패 시 None.
    정본은 트랙A가 순차 편집 중일 수 있음 → 로드 실패/깨진 HTML이면 30s 대기 후 재시도(최대 3회).
    """
    from playwright.sync_api import sync_playwright

    exe = find_cached_chromium()
    url = f"http://localhost:{PORT}/map_leaflet.html?region={lawd}"
    ls_key = f"jimeok.competitors.{lawd}"
    payload = json.dumps(
        [{"i": s["i"], "name": s["name"], "color": s["color"], "order": s["order"]} for s in sel],
        ensure_ascii=False)

    with sync_playwright() as p:
        launch_kw = {"headless": True}
        if exe:
            launch_kw["executable_path"] = exe
            m = re.search(r"chromium-(\d+)", exe)
            print(f"✅ chromium 캐시 재사용: chromium-{m.group(1) if m else '?'}")
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=scale)
        # 스키마 계약: localStorage["jimeok.competitors.<lawd>"] = [{i,name,color,order}]
        ctx.add_init_script(
            "try { localStorage.setItem(%s, %s); } catch (e) {}"
            % (json.dumps(ls_key), json.dumps(payload)))
        page = ctx.new_page()

        loaded = False
        for attempt in range(1, 4):
            try:
                page.goto(url, wait_until="networkidle", timeout=90000)
                ok = page.evaluate(
                    "!!(window.MAP_DATA && window.MAP_DATA.meta && document.getElementById('map'))")
                if ok:
                    loaded = True
                    break
                print(f"⚠️  로드 실패(데이터/맵 없음) — 시도 {attempt}/3")
            except Exception as e:
                print(f"⚠️  로드 실패({type(e).__name__}) — 시도 {attempt}/3")
            if attempt < 3:
                print("   정본(트랙A) 편집 중 가능성 — 30s 대기 후 재시도")
                time.sleep(30)
        if not loaded:
            browser.close()
            die("map_leaflet.html 로드 3회 실패 — 정본 상태 확인 후 재실행")

        # 타일 로드 대기: networkidle + 2s (요구사항 ②)
        page.wait_for_timeout(2000)
        tiles = page.evaluate("document.querySelectorAll('.leaflet-tile-loaded').length")
        print(f"   타일 로드: {tiles}장" + ("" if tiles else " ⚠️ 0장 — 타일 서버 연결 확인"))

        # ③ 베이스맵 설정 (정본 setBasemap 전역 함수)
        page.evaluate("(k) => { if (typeof setBasemap === 'function') setBasemap(k); }", basemap)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        bounds = page.evaluate(
            "() => { try { const b = map.getBounds(); "
            "return {n: b.getNorth(), s: b.getSouth(), e: b.getEast(), w: b.getWest()}; "
            "} catch (e) { return null; } }")
        page.locator("#map").screenshot(path=str(out_png))
        print(f"✅ 지도 캡처: {out_png.name} ({VIEWPORT['width']}×{VIEWPORT['height']} @{scale}x, 베이스맵 {basemap})")
        browser.close()
        return bounds


# ─────────────────────────── 보드 (④ 스크립트 측 조립) ───────────────────────────

def stat_rows_flat(stats: dict) -> list[dict]:
    out = []
    for a in stats["apts"]:
        for r in a["rows"]:
            out.append({"apt": a, "row": r})
    return out


def build_board_html(stats: dict, bundle: dict, lawd: str, map_png: Path) -> str:
    """지도 PNG(base64 내장) + 통계표 나란히 — 인라인 CSS, 스크린 매체 스타일."""
    b64 = base64.b64encode(map_png.read_bytes()).decode()
    meta = bundle.get("meta", {})
    region = meta.get("region", lawd)
    direct = "직거래 제외" if stats["exclude_direct"] else "직거래 포함"
    period = f"{stats['period_start']} ~ {stats['max_date']} ({stats['period_months']}개월)"

    trs = []
    for a in stats["apts"]:
        first = True
        for r in a["rows"]:
            pk, at = r["peak"], r["alltime"]
            apt_cell = ""
            if first:
                apt_cell = (
                    f'<td class="apt" rowspan="{len(a["rows"])}">'
                    f'<span class="dot" style="background:{a["color"]}"></span>'
                    f'<div><b>{html_mod.escape(a["name"])}</b>'
                    f'<span class="sub">{html_mod.escape(a["apt"].get("dong") or "")} · '
                    f'window {a["win_total"]}건</span></div></td>')
                first = False
            trs.append(
                "<tr>" + apt_cell +
                f'<td>{html_mod.escape(r["type"])}</td>'
                f'<td class="num">{fmt1(r["avg"])}</td>'
                f'<td class="num">{fmt1(r["median"])}</td>'
                f'<td class="num">{fmt_int(r["count"])}</td>'
                f'<td class="num">{fmt_int(pk["amt"]) if pk else "—"}</td>'
                f'<td>{pk["date"] if pk else "—"}</td>'
                f'<td class="num">{fmt_int(at["amt"]) if at else "—"}</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>경쟁단지 보드 {lawd}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Pretendard','Apple SD Gothic Neo','-apple-system',sans-serif;
         background:#F4F6F8; color:#1A2230; padding:32px; }}
  .board {{ display:flex; gap:24px; align-items:stretch; max-width:1480px; }}
  .head {{ margin-bottom:20px; }}
  .head h1 {{ font-size:26px; font-weight:700; letter-spacing:-.02em; }}
  .head .sub {{ margin-top:6px; font-size:13px; color:#5A6472; }}
  .head .chip {{ display:inline-block; background:#E8F1FC; color:#0071E3; font-weight:600;
                 font-size:12px; padding:4px 10px; border-radius:99px; margin-right:6px; }}
  .map {{ flex:0 0 860px; }}
  .map img {{ width:860px; border-radius:14px; display:block; }}
  .panel {{ flex:1; background:#FFFFFF; border-radius:14px; padding:24px;
            display:flex; flex-direction:column; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
  th {{ text-align:left; color:#8A94A6; font-size:11px; font-weight:600; padding:6px 8px;
        border-bottom:1px solid #E4E8EE; white-space:nowrap; }}
  td {{ padding:8px 8px; border-bottom:1px solid #EEF1F5; vertical-align:middle; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-family:'Inter','Pretendard',sans-serif; }}
  td.apt {{ width:190px; }}
  td.apt b {{ font-size:13px; display:block; }}
  td.apt .sub {{ font-size:11px; color:#8A94A6; }}
  .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px;
          vertical-align:middle; }}
  .foot {{ margin-top:auto; padding-top:14px; font-size:11px; color:#8A94A6; }}
</style></head><body>
  <div class="head">
    <h1>경쟁단지 비교 — {html_mod.escape(region)} ({lawd})</h1>
    <div class="sub">
      <span class="chip">{period}</span>
      <span class="chip">{direct}</span>
      <span class="chip">기준 {html_mod.escape(str(meta.get('generated_at', '')))}</span>
    </div>
  </div>
  <div class="board">
    <div class="map"><img src="data:image/png;base64,{b64}" alt="map"></div>
    <div class="panel">
      <table>
        <thead><tr><th>단지</th><th>타입</th><th style="text-align:right">평당가 평균</th>
        <th style="text-align:right">중위</th><th style="text-align:right">거래량</th>
        <th style="text-align:right">최고점(만원)</th><th>최고점 날짜</th>
        <th style="text-align:right">역대 최고(만원)</th></tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>
      <div class="foot">평당가 단위: 만원/평 · 표본 &lt;3건 타입은 "기타" 합산 ·
      역대 최고 = 기간 무관 전체 데이터 최고점<br>
      출처: 국토교통부 실거래가 (기준 {html_mod.escape(str(meta.get('generated_at', '')))})</div>
    </div>
  </div>
</body></html>"""


def capture_board(ctx, board_html_path: Path, out_png: Path, scale: int) -> None:
    page = ctx.new_page()
    page.set_viewport_size({"width": 1560, "height": 1000})
    page.goto(board_html_path.as_uri(), wait_until="load")
    page.wait_for_timeout(400)  # 웹폰트 없이 시스템 폰트 — 짧은 정착 대기
    page.screenshot(path=str(out_png), full_page=True)
    page.close()
    print(f"✅ 보드 캡처: {out_png.name} @{scale}x")


# ─────────────────────────── xlsx · csv ───────────────────────────

def write_xlsx(path: Path, stats: dict, bundle: dict, lawd: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    meta = bundle.get("meta", {})
    wb = Workbook()

    # 시트1: 경쟁단지 비교
    ws = wb.active
    ws.title = "경쟁단지 비교"
    headers = ["단지", "타입", "평당가 평균(만원/평)", "중위(만원/평)", "거래량",
               "최고점(만원)", "최고점 날짜", "역대 최고(만원)"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8F1FC")
        c.alignment = Alignment(horizontal="center")
    for a in stats["apts"]:
        for r in a["rows"]:
            pk, at = r["peak"], r["alltime"]
            ws.append([
                a["name"], r["type"],
                round_half_up(r["avg"], 1) if r["avg"] is not None else "표본 없음",
                round_half_up(r["median"], 1) if r["median"] is not None else "표본 없음",
                r["count"],
                pk["amt"] if pk else None, pk["date"] if pk else None,
                at["amt"] if at else None])
    for col, w in zip("ABCDEFGH", (28, 16, 18, 16, 9, 13, 13, 15)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"

    # 시트2: 메타
    ws2 = wb.create_sheet("메타")
    direct = "ON (deal_gbn='직거래' 전 집계 제외)" if stats["exclude_direct"] else "OFF"
    meta_rows = [
        ("출처", "국토교통부 실거래가 (RTMS 매매)"),
        ("기준일", str(meta.get("generated_at", "")) + " (번들 meta.generated_at)"),
        ("지역", f'{meta.get("region", lawd)} ({lawd})'),
        ("기간", f"{stats['period_start']} ~ {stats['max_date']} "
                f"({stats['period_months']}개월, 시작일 비포함)"),
        ("직거래 제외", direct),
        ("중복 제거 방식", "8필드 UNIQUE 내장 (db.py idx_trade_unique_v2: lawd_cd·apt_name·"
                      "legal_dong·jibun·exclu_area·deal_amount·deal_date·floor) — 수집 단계 적용, 통계 단계 별도 dedup 없음"),
        ("산식 기준", "_디자인기획_지도디테일_v3.md §6"),
        ("선택 단지", ", ".join(f'{a["order"]}.{a["name"]}' for a in stats["apts"])),
        ("생성 시각", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("생성 도구", "export_deliverables.py (플랜 jimeok-v3 T11)"),
    ]
    for k, v in meta_rows:
        ws2.append([k, v])
        ws2.cell(ws2.max_row, 1).font = Font(bold=True)
    ws2.column_dimensions["A"].width = 14
    ws2.column_dimensions["B"].width = 110

    # 시트3: 타입 상세 (window 내 개별 거래 — 직거래 플래그·집계 포함 여부 표시)
    ws3 = wb.create_sheet("타입 상세")
    ws3.append(["단지", "타입", "계약일", "전용㎡", "평", "금액(만원)", "층",
                "평당가(만원/평)", "직거래", "집계 포함"])
    for c in ws3[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E8F1FC")
    for col, w in zip("ABCDEFGHIJ", (28, 14, 12, 10, 8, 12, 6, 16, 8, 10)):
        ws3.column_dimensions[col].width = w
    ws3.freeze_panes = "A2"

    wb.save(path)


def fill_type_detail(path: Path, stats: dict, lawd: str, conn: sqlite3.Connection,
                     period_start: str, max_date: str) -> None:
    """시트3에 window 내 개별 거래 적재 (직거래로 제외된 행도 플래그로 표시 — 감사용)."""
    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws3 = wb["타입 상세"]
    for a in stats["apts"]:
        apt = a["apt"]
        rows = conn.execute(
            "SELECT exclu_area, pyeong, deal_amount, deal_date, floor, deal_gbn "
            "FROM trades WHERE lawd_cd = ? AND apt_name = ? AND deal_date > ? AND deal_date <= ? "
            "AND exclu_area IS NOT NULL AND pyeong > 0 AND deal_amount IS NOT NULL "
            "ORDER BY deal_date DESC",
            (lawd, apt["name"], period_start, max_date)).fetchall()
        # dong 필터 (compute_stats와 동일 로직)
        if apt.get("dong"):
            dong_rows = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE lawd_cd=? AND apt_name=? AND legal_dong=?",
                (lawd, apt["name"], apt["dong"])).fetchone()[0]
            if dong_rows:
                rows = [r for r in conn.execute(
                    "SELECT exclu_area, pyeong, deal_amount, deal_date, floor, deal_gbn "
                    "FROM trades WHERE lawd_cd=? AND apt_name=? AND legal_dong=? "
                    "AND deal_date > ? AND deal_date <= ? "
                    "AND exclu_area IS NOT NULL AND pyeong > 0 AND deal_amount IS NOT NULL "
                    "ORDER BY deal_date DESC",
                    (lawd, apt["name"], apt["dong"], period_start, max_date)).fetchall()]
        # window 내 타입별 표본 수 (직거래 제외 후) — "집계 포함" 판정용
        kept = [r for r in rows if not (stats["exclude_direct"] and r["deal_gbn"] == "직거래")]
        cnt: dict[int, int] = {}
        for r in kept:
            cnt[math.floor(r["exclu_area"])] = cnt.get(math.floor(r["exclu_area"]), 0) + 1
        for r in rows:
            key = math.floor(r["exclu_area"])
            direct = r["deal_gbn"] == "직거래"
            included = (not (stats["exclude_direct"] and direct)) and cnt.get(key, 0) >= 3
            label = f"{key}㎡ (≈{int(round_half_up(key * 0.3025))}평)"
            ws3.append([a["name"], label, r["deal_date"], r["exclu_area"], r["pyeong"],
                        r["deal_amount"], r["floor"],
                        round_half_up(r["deal_amount"] / r["pyeong"], 1),
                        "Y" if direct else "", "Y" if included else "N"])
    wb.save(path)


def write_csv(path: Path, stats: dict, lawd: str, conn: sqlite3.Connection,
              period_start: str, max_date: str) -> int:
    """⑥ 원시거래 CSV: window 내 선택 단지 거래 (직거래 플래그 포함, 배제 안 함 — 원시 그대로)."""
    import csv
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["단지", "법정동", "계약일", "전용㎡", "평", "금액만원", "층", "직거래"])
        for a in stats["apts"]:
            apt = a["apt"]
            rows = conn.execute(
                "SELECT apt_name, legal_dong, deal_date, exclu_area, pyeong, deal_amount, floor, deal_gbn "
                "FROM trades WHERE lawd_cd=? AND apt_name=? AND deal_date > ? AND deal_date <= ? "
                "ORDER BY deal_date", (lawd, apt["name"], period_start, max_date)).fetchall()
            if apt.get("dong") and any(r["legal_dong"] == apt["dong"] for r in rows):
                rows = [r for r in rows if r["legal_dong"] == apt["dong"]]
            for r in rows:
                w.writerow([r["apt_name"], r["legal_dong"] or "", r["deal_date"],
                            r["exclu_area"], r["pyeong"], r["deal_amount"], r["floor"],
                            "Y" if r["deal_gbn"] == "직거래" else ""])
                n += 1
    return n


# ─────────────────────────── PPTX (⑦) ───────────────────────────

def write_pptx(path: Path, map_png: Path, bounds: dict | None, stats: dict,
               bundle: dict, lawd: str) -> int:
    """16:9 슬라이드: 지도 풀블리드 + 경쟁단지 링(바운드→픽셀 선형 변환) + 번호 + 출처."""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE

    meta = bundle.get("meta", {})
    img_w, img_h = png_size(map_png)
    SW, SH = Inches(13.333), Inches(7.5)  # 16:9

    prs = Presentation()
    prs.slide_width, prs.slide_height = SW, SH
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    # 풀블리드 배경 (cover 핏 — 넘치는 부분은 슬라이드 밖으로)
    k = max(SW / img_w, SH / img_h)          # 인치/픽셀
    pw, ph = img_w * k, img_h * k
    left, top = (SW - pw) / 2, (SH - ph) / 2
    slide.shapes.add_picture(str(map_png), int(left), int(top), width=int(pw), height=int(ph))

    def px2in_x(px: float):
        return left + px * k

    def px2in_y(py: float):
        return top + py * k

    n_shapes = 1
    if bounds and bounds.get("n") is not None:
        n_, s_, e_, w_ = bounds["n"], bounds["s"], bounds["e"], bounds["w"]
        for a in stats["apts"]:
            apt = a["apt"]
            lat, lng = apt.get("lat"), apt.get("lng")
            if lat is None or lng is None:
                print(f"⚠️  {a['name']}: 좌표 없음 → 링 스킵")
                continue
            # lat/lng → 픽셀 선형 변환 (캡처 시점 map.getBounds() 기준)
            px = (lng - w_) / (e_ - w_) * img_w
            py = (n_ - lat) / (n_ - s_) * img_h
            # 300m → 픽셀: 경도 1도 = 111320·cos(lat) m
            m_per_px = M_PER_DEG_LAT * math.cos(math.radians(lat)) * (e_ - w_) / img_w
            r_px = CMP_RADIUS_M / m_per_px
            r_in = r_px * k
            cx, cy = px2in_x(px), px2in_y(py)
            color = RGBColor.from_string(a["color"].lstrip("#"))

            ring = slide.shapes.add_shape(MSO_SHAPE.OVAL, int(cx - r_in), int(cy - r_in),
                                          int(2 * r_in), int(2 * r_in))
            ring.fill.background()
            ring.line.color.rgb = color
            ring.line.width = Pt(2.25)  # 스펙 §4 weight 3px ≈ 2.25pt
            n_shapes += 1

            # 번호 배지: 지름 20px 원 · 배경 단지색 · 흰 숫자 · 링 상단 (스펙 §4)
            d = 20 * k
            badge = slide.shapes.add_shape(MSO_SHAPE.OVAL, int(cx - d / 2),
                                           int(cy - r_in - d / 2), int(d), int(d))
            badge.fill.solid()
            badge.fill.fore_color.rgb = color
            badge.line.fill.background()
            tf = badge.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            para = tf.paragraphs[0]
            para.alignment = PP_ALIGN.CENTER
            run = para.add_run()
            run.text = str(a["order"])
            run.font.size = Pt(8.5)  # 11px ≈ 8.25pt
            run.font.bold = True
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            n_shapes += 1

        # 사업지 핀 + 반경원 3개 — meta.site 존재 시에만 (region 번들은 null이라 스킵)
        site = meta.get("site")
        if isinstance(site, dict) and site.get("lat") is not None and site.get("lng") is not None:
            slat, slng = site["lat"], site["lng"]
            spx = (slng - w_) / (e_ - w_) * img_w
            spy = (n_ - slat) / (n_ - s_) * img_h
            m_per_px = M_PER_DEG_LAT * math.cos(math.radians(slat)) * (e_ - w_) / img_w
            for rm in SITE_RADII_M:
                r_in = (rm / m_per_px) * k
                cx, cy = px2in_x(spx), px2in_y(spy)
                circ = slide.shapes.add_shape(MSO_SHAPE.OVAL, int(cx - r_in), int(cy - r_in),
                                              int(2 * r_in), int(2 * r_in))
                circ.fill.background()
                circ.line.color.rgb = RGBColor(0x5A, 0x64, 0x72)
                circ.line.width = Pt(1)
                n_shapes += 1
            pin_d = 16 * k
            pin = slide.shapes.add_shape(MSO_SHAPE.OVAL, int(px2in_x(spx) - pin_d / 2),
                                         int(px2in_y(spy) - pin_d / 2), int(pin_d), int(pin_d))
            pin.fill.solid()
            pin.fill.fore_color.rgb = RGBColor(0x00, 0x71, 0xE3)
            pin.line.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            pin.line.width = Pt(1.5)
            n_shapes += 1
    else:
        print("⚠️  바운드 미확보 — PPTX 링/라벨 없이 지도만 배치")

    # 제목 칩 (좌상단)
    title_box = slide.shapes.add_textbox(Inches(0.3), Inches(0.25), Inches(5.2), Inches(0.7))
    tf = title_box.text_frame
    tf.word_wrap = True
    p1 = tf.paragraphs[0]
    r1 = p1.add_run()
    r1.text = f'{meta.get("region", lawd)} 경쟁단지 지도'
    r1.font.size = Pt(18)  # 스펙 §2 캡처 오버레이 제목 18px
    r1.font.bold = True
    r1.font.color.rgb = RGBColor(0x1A, 0x22, 0x30)
    p2 = tf.add_paragraph()
    r2 = p2.add_run()
    r2.text = (f'{stats["period_start"]} ~ {stats["max_date"]} ({stats["period_months"]}개월) · '
               f'{"직거래 제외" if stats["exclude_direct"] else "직거래 포함"}')
    r2.font.size = Pt(11)
    r2.font.color.rgb = RGBColor(0x5A, 0x64, 0x72)
    n_shapes += 1

    # 출처 텍스트 (우하단) — 스펙 출처 표기 정본 문구
    src_box = slide.shapes.add_textbox(SW - Inches(5.0), SH - Inches(0.62),
                                       Inches(4.7), Inches(0.4))
    tf = src_box.text_frame
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    r = p.add_run()
    r.text = f'출처: 국토교통부 실거래가 (기준 {meta.get("generated_at", "")})'
    r.font.size = Pt(10)
    r.font.color.rgb = RGBColor(0x5A, 0x64, 0x72)
    n_shapes += 1

    prs.save(path)
    return n_shapes


def verify_pptx(path: Path) -> tuple[int, int]:
    """재열기 파싱 검증 — (슬라이드 수, 도형 수) 반환. 파싱 실패 시 예외."""
    from pptx import Presentation
    prs = Presentation(str(path))
    slides = len(prs.slides._sldIdLst)
    shapes = sum(len(s.shapes) for s in prs.slides)
    return slides, shapes


# ─────────────────────────── 메인 ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="경쟁단지 비교 산출물 로컬 생성 (스펙 v3 §6 정본 산식)",
        epilog=USAGE_EXAMPLE, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lawd", required=True, help="5자리 법정동코드 (예 11590)")
    ap.add_argument("--selection", required=True, help="selection JSON 파일 경로")
    ap.add_argument("--out", required=True, help="산출물 출력 폴더")
    ap.add_argument("--scale", type=int, default=2, choices=(2, 3), help="deviceScaleFactor (기본 2)")
    ap.add_argument("--basemap", default="minimal",
                    choices=("minimal", "voyager", "dark", "sat"), help="베이스맵 (스펙 §5 캡처 4종)")
    ap.add_argument("--period", type=int, default=12, choices=(3, 6, 12, 24),
                    help="통계 기간 개월 (스펙 §6.4, 기본 12)")
    ap.add_argument("--project", default=None, help="사업명 — _projects/<사업명>/.dev/ 로 산출물 복사")
    ap.add_argument("--include-direct", action="store_true",
                    help="직거래 포함 (기본: 직거래 제외 ON, 스펙 §6.6)")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{5}", args.lawd):
        die("--lawd 는 5자리 숫자여야 합니다 (예 11590)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 입력 로드 ──
    bundle = load_bundle(args.lawd)
    meta = bundle.get("meta", {})
    apts = bundle.get("apts", [])
    if not apts:
        die("번들에 apts가 없습니다")
    sel = load_selection(args.selection, apts)
    print(f"✅ selection: {len(sel)}개 단지 — " +
          ", ".join(f'{s["order"]}.{s["name"]}' for s in sel))

    conn = open_db_ro(args.lawd)
    exclude_direct = not args.include_direct

    # ── ⑤ 통계 (스펙 §6) ──
    stats = compute_stats(conn, args.lawd, sel, args.period, exclude_direct)
    print(f"✅ 통계: 기준일(max_date) {stats['max_date']} · 기간 {stats['period_start']}~"
          f"{stats['max_date']} ({args.period}개월) · 직거래 {'제외' if exclude_direct else '포함'} · "
          f"window 거래 {stats['window_total']}건")

    # ── ① 서버 (curl-first, 중복 기동 금지) ──
    server = ensure_server()

    png_map = out_dir / f"지먹_{args.lawd}_지도_{ts}.png"
    png_board = out_dir / f"지먹_{args.lawd}_보드_{ts}.png"
    xlsx = out_dir / f"경쟁단지_{args.lawd}_{ts}.xlsx"
    csvf = out_dir / f"경쟁단지_{args.lawd}_{ts}_원시거래.csv"
    pptxf = out_dir / f"지먹_{args.lawd}_경쟁지도_{ts}.pptx"
    board_html = out_dir / f"지먹_{args.lawd}_보드_{ts}.html"

    try:
        # ── ②③ 지도 캡처 (selection 주입 + 베이스맵 + #map 스크린샷) ──
        bounds = capture_map(args.lawd, sel, args.basemap, args.scale, png_map)

        # ── ④ 보드 (지도 PNG + 통계표 조립 HTML → 스크린샷) ──
        board_html.write_text(build_board_html(stats, bundle, args.lawd, png_map),
                              encoding="utf-8")
        # 보드 캡처용 브라우저 재사용 (서버/캡처와 별도 컨텍스트)
        from playwright.sync_api import sync_playwright
        exe = find_cached_chromium()
        with sync_playwright() as p:
            kw = {"headless": True}
            if exe:
                kw["executable_path"] = exe
            b = p.chromium.launch(**kw)
            ctx = b.new_context(device_scale_factor=args.scale)
            capture_board(ctx, board_html, png_board, args.scale)
            b.close()

        # ── ⑤ xlsx ──
        write_xlsx(xlsx, stats, bundle, args.lawd)
        fill_type_detail(xlsx, stats, args.lawd, conn, stats["period_start"], stats["max_date"])
        print(f"✅ xlsx: {xlsx.name} (시트3 타입 상세 {sum(a['win_total'] for a in stats['apts'])}건)")

        # ── ⑥ 원시거래 CSV ──
        n_csv = write_csv(csvf, stats, args.lawd, conn, stats["period_start"], stats["max_date"])
        print(f"✅ csv: {csvf.name} ({n_csv}건)")

        # ── ⑦ PPTX ──
        n_shapes = write_pptx(pptxf, png_map, bounds, stats, bundle, args.lawd)
        print(f"✅ pptx: {pptxf.name} (도형 {n_shapes}개)")
        v_slides, v_shapes = verify_pptx(pptxf)
        print(f"✅ PPTX 재열기 파싱: 0에러 (슬라이드 {v_slides} · 도형 {v_shapes})")

        # ── ⑧ 프로젝트 복사 (폴더 없으면 경고 후 스킵 — 신규 생성 절대 금지) ──
        if args.project:
            dest = PROJECTS_ROOT / args.project / ".dev"
            if dest.is_dir():
                for f in (png_map, png_board, xlsx, csvf, pptxf, board_html):
                    shutil.copy2(f, dest / f.name)
                print(f"✅ 프로젝트 복사: {dest} (6개 파일)")
            else:
                print(f"⚠️  프로젝트 폴더 없음 → 복사 스킵: {dest} (신규 생성 금지 규칙)")

        # ── ⑨ stdout 통계 출력 + 교차 검증 ──
        print("\n── 통계 요약 (스펙 v3 §6) ────────────────────────────")
        print(f"{'단지':<24} {'타입':<14} {'평균':>10} {'중위':>10} {'거래량':>6} "
              f"{'최고점(만원)':>12} {'최고점 날짜':<12} {'역대 최고(만원)':>14}")
        for a in stats["apts"]:
            for r in a["rows"]:
                pk, at = r["peak"], r["alltime"]
                print(f"{a['name']:<24} {r['type']:<14} {fmt1(r['avg']):>10} {fmt1(r['median']):>10} "
                      f"{fmt_int(r['count']):>6} {fmt_int(pk['amt']) if pk else '—':>12} "
                      f"{pk['date'] if pk else '—':<12} {fmt_int(at['amt']) if at else '—':>14}")

        print("\n── 교차 검증 (독립 SQL 재계산 vs Python 집계) ──────────")
        cross_check_sql(conn, args.lawd, stats)
        print("브라우저 교차검증 대기(T9 미구현)")

        print("\n── 산출물 ─────────────────────────────────────────")
        for f in (png_map, png_board, xlsx, csvf, pptxf, board_html):
            print(f"  {f}  ({f.stat().st_size:,}B)")
        return 0
    finally:
        conn.close()
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            print("✅ 서버 종료 (이 세션이 기동한 것만)")


if __name__ == "__main__":
    sys.exit(main())
