"""
_임대분석_시안.py — 주변 단지 임대분석 매트릭스 (평형 × 연차) 프로토타입
================================================================
목적: map_leaflet.html에 넣을 "주변 임대시장 분석" 기능의 계산 로직을
     본 코드로 먼저 검증한다. 여기서 나온 숫자가 곧 지도 패널에 들어갈 값.

데이터: 같은 폴더의 map_data.js (window.MAP_DATA) — 재수집 0.
     rents 레코드: {a, ar(전용㎡), py(전용평), dep(보증금만), mr(월세만·0이면 전세), d, fl, by(건축년도), ct}
     trades 레코드: {a, ar, py, amt(매매가만), d, fl, by}

확정 설계 (2026-07-17 부장 결정):
  · 평형 밴드(전용㎡): 25평형=50~64㎡(59타입) · 34평형=74~89㎡(84타입)
  · 연차(2026 기준): 신축 ≤5년(2021+) · 준신축 6~10년(2016~20) · 구축 11년+(~2015)
  · 형태: 평형 × 연차 매트릭스, 셀마다 평균월세·전세가율·월세수익률·표본수

지표 공식:
  · 평균월세  = median(mr>0)                     (셀 내)
  · 전세가율  = median(전세보증금 mr==0) / median(매매가) × 100
  · 월세수익률 = (평균월세 × 12) / (median매매가 − median월세보증금) × 100

가드레일 (킹동산 3,600% 발산 방지):
  · 표본 최소치 MIN_N=3 (매/월/전 각각)
  · 분모 하한: (매매 − 월세보증금) ≥ 매매 × 5% 미만이면 '발산컷'
  · 이상치 캡: 월세수익률 > 15% 는 ⚠️ 플래그

윈도우: 최근 12개월(데이터 최신일 기준). ※킹동산은 3개월 — 매트릭스 윈도우는 내일 재결정.

사용: python _임대분석_시안.py
"""
import json, datetime, statistics as st
from pathlib import Path

# ── 설계 파라미터 ────────────────────────────────────────────
NOW_Y = 2026
MIN_N = 3            # 셀당 최소 표본
YIELD_CAP = 15.0     # 월세수익률 이상치 상한(%)
DENOM_FLOOR = 0.05   # 분모 하한 = 매매가 × 5%
WINDOW_DAYS = 365    # 최근 12개월

PB = ["25평형(59㎡)", "34평형(84㎡)"]
YB = ["신축", "준신축", "구축"]


def pyeong_bucket(ar):          # 전용㎡ → 대표 평형 밴드
    if 50 <= ar <= 64: return "25평형(59㎡)"
    if 74 <= ar <= 89: return "34평형(84㎡)"
    return None


def year_bucket(by):            # 건축년도 → 연차 구간
    if not by: return None
    age = NOW_Y - by
    if age <= 5:  return "신축"
    if age <= 10: return "준신축"
    return "구축"


def load_map_data(path="map_data.js"):
    raw = Path(path).read_text(encoding="utf-8")
    raw = raw[raw.index("{"): raw.rstrip().rstrip(";").rindex("}") + 1]
    return json.loads(raw)


def build_matrix(D):
    trades, rents = D["trades"], D["rents"]
    alld = [x["d"] for x in trades + rents if x["d"]]
    maxd = max(alld)
    cut = (datetime.date.fromisoformat(maxd) - datetime.timedelta(days=WINDOW_DAYS)).isoformat()

    cell = {(p, y): {"매매": [], "전세": [], "월세": [], "월세보증": []} for p in PB for y in YB}
    for t in trades:
        if t["d"] < cut: continue
        p, y = pyeong_bucket(t["ar"]), year_bucket(t.get("by"))
        if p and y: cell[(p, y)]["매매"].append(t["amt"])
    for r in rents:
        if r["d"] < cut: continue
        p, y = pyeong_bucket(r["ar"]), year_bucket(r.get("by"))
        if not (p and y): continue
        if r["mr"] == 0:
            cell[(p, y)]["전세"].append(r["dep"])
        else:
            cell[(p, y)]["월세"].append(r["mr"])
            cell[(p, y)]["월세보증"].append(r["dep"])
    return cell, cut, maxd


def med(x): return st.median(x) if x else None


def cell_metrics(cd):
    mm, wr, wd, jn = med(cd["매매"]), med(cd["월세"]), med(cd["월세보증"]), med(cd["전세"])
    n_mm, n_wr, n_jn = len(cd["매매"]), len(cd["월세"]), len(cd["전세"])

    jr = f"{round(jn/mm*100)}%" if (jn and mm and n_jn >= MIN_N and n_mm >= MIN_N) else "·"
    ws = f"{wr:.0f}만" if (wr is not None and n_wr >= MIN_N) else "·"

    yld = "·"
    if wr is not None and mm and wd is not None and n_wr >= MIN_N and n_mm >= MIN_N:
        denom = mm - wd
        if denom < mm * DENOM_FLOOR:
            yld = "⚠️발산컷"
        else:
            v = (wr * 12) / denom * 100
            yld = f"{v:.1f}%" + ("⚠️" if v > YIELD_CAP else "")
    return ws, jr, yld, f"매{n_mm}/월{n_wr}/전{n_jn}"


def main():
    D = load_map_data()
    meta = D["meta"]
    cell, cut, maxd = build_matrix(D)
    print(f"지역: {meta['region']} | 매매 {len(D['trades']):,} · 전월세 {len(D['rents']):,}")
    print(f"윈도우: {cut} ~ {maxd} (최근 12개월)\n")
    print(f"{'평형':<14}{'연차':<7}{'평균월세':<9}{'전세가율':<8}{'월세수익률':<11}{'표본(매/월/전)'}")
    print("─" * 62)
    for p in PB:
        for y in YB:
            ws, jr, yld, n = cell_metrics(cell[(p, y)])
            print(f"{p:<14}{y:<7}{ws:<9}{jr:<8}{yld:<11}{n}")
        print()


if __name__ == "__main__":
    main()
