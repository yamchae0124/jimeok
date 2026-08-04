#!/usr/bin/env bash
# sync_deploy.sh — 정본을 _deploy/ 번들로 동기화 (배포 레벨 A + B)
# 기획 근거: _기획_배포_20260721.md §4~5 / _기획_시각컨트롤_지먹_20260731.md §5
#
# 레벨 A: 루트 9종 js + map_leaflet.html(뷰어 겸용, ?region= 지원)
# 레벨 B: regions/ 전체 미러 (index.json + regions/<lawd>/map_data.js × N)
#
# _deploy/index.html(허브) · _deploy/DEPLOY.md 는 **이 폴더 정본** — sync가 안 건드림.
# 허용 목록(allowlist)만 복사. data/*.db · *.py · .env · _archive 는 절대 복사 안 함.
# 사용: bash sync_deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

DEPLOY="_deploy"
mkdir -p "$DEPLOY"

# ── 허용 파일 명시 (정본 HTML/뷰어 + 데이터 js 9종) ──
# map_leaflet.html = 단일지역 정본이자 ?region= 뷰어 겸용 (부트스트랩 내장).
ALLOW=(
  "map_leaflet.html"
  "map_data.js"
  "transit.js"
  "infra.js"
  "infra_icons.js"
  "lh.js"
  "commute.js"
  "district.js"
  "admin.js"
  "gdc_data.js"
)

# ── 안전장치: 금지 패턴이 허용 목록에 섞이면 즉시 중단 ──
for f in "${ALLOW[@]}"; do
  case "$f" in
    *.db|*.py|.env*|*_archive*|*secret*|*key*)
      echo "❌ 금지 파일이 허용 목록에 포함됨: $f — 중단" >&2; exit 1 ;;
  esac
  if [[ ! -f "$f" ]]; then
    echo "⚠️  정본에 없음 (스킵): $f" >&2; continue
  fi
  cp -f "$f" "$DEPLOY/$f"
  echo "  ✓ $f"
done

# ── 레벨 B: regions/ 미러 (index.json + regions/<lawd>/map_data.js) ──
# build_hub.py 산출물. 없으면 레벨 A만 동기화 (허브는 fetch 실패 상태로 안내).
if [[ -f "regions/index.json" ]]; then
  mkdir -p "$DEPLOY/regions"
  rsync -a --delete regions/ "$DEPLOY/regions/"
  n_regions=$(find "$DEPLOY/regions" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')
  echo "  ✓ regions/ 미러 ($n_regions 개 지역 번들 + index.json)"
else
  echo "⚠️  regions/index.json 없음 — 레벨 B 스 (python build_hub.py 먼저 실행)" >&2
fi

# ── 보안 재확인: 배포 번들(루트 + 전 region)에 카카오 키 잔존 0 ──
if grep -qiE "kakao|rest_api_key|js_key" "$DEPLOY/map_data.js" 2>/dev/null; then
  echo "❌ _deploy/map_data.js 에 키 문자열 잔존 — 배포 중단" >&2; exit 1
fi
if grep -rqiE "kakao|rest_api_key|js_key" "$DEPLOY/regions/"*/map_data.js 2>/dev/null; then
  echo "❌ _deploy/regions/*/map_data.js 에 키 문자열 잔존 — 배포 중단" >&2; exit 1
fi

# ── _deploy 정본 파일 존재 확인 (허브·문서는 sync가 안 만듦) ──
for fixed in index.html DEPLOY.md; do
  if [[ -f "$DEPLOY/$fixed" ]]; then
    echo "  ✓ $fixed (정본 유지)"
  else
    echo "⚠️  $DEPLOY/$fixed 없음 — 허브/문서는 이 폴더 정본이라 sync가 생성 안 함" >&2
  fi
done

echo ""
echo "✅ 동기화 완료 → $DEPLOY/"
echo "   배포: $DEPLOY/ 폴더를 Cloudflare Pages / Vercel 에 정적 업로드 (DEPLOY.md 참고)"
echo "   허브: index.html  ·  뷰어: map_leaflet.html?region=<lawd>"
