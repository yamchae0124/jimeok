# vendor/ — 로컬 라이브러리 (CDN 런타임 의존 0 · 파일://·오프라인 대응)

map_leaflet.html 맨 끝 가드 태그(`<script src="vendor/..." onerror="void 0">`)가 로드.
전부 MIT 라이선스. 플랜 jimeok-v3 T14에서 커밋 시점(2026-08-05) 최신 안정판 고정.

| 파일 | 라이브러리 | 버전 | 라이선스 | 출처 |
|---|---|---|---|---|
| html2canvas.min.js | html2canvas | 1.4.1 | MIT | https://unpkg.com/html2canvas@1.4.1/dist/html2canvas.min.js |
| xlsx.full.min.js | SheetJS (xlsx) | 0.20.3 | Apache-2.0 (MIT 호환) | https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js |
| pptxgenjs.cjs.min.js | PptxGenJS | 3.12.0 | MIT | https://unpkg.com/pptxgenjs@3.12.0/dist/pptxgen.bundle.js |

주의:
- `pptxgenjs.cjs.min.js` 파일명은 T10이 커밋한 정본 태그 계약(`vendor/pptxgenjs.cjs.min.js`) 유지용.
  실제 내용은 **브라우저 번들 빌드(pptxgen.bundle.js)** — JSZip 내장 자립형이라 전역 `PptxGenJS` 노출.
  npm의 `pptxgen.cjs.js`(CommonJS)는 브라우저 `<script>`에서 `exports` 미정의로 동작 불가,
  `pptxgen.min.js`는 JSZip 외부 의존이라 배제. unpkg에 `pptxgenjs.cjs.min.js` 원본 파일은 존재하지 않음.
- SheetJS 0.20.x는 npm 미배포(공식 CDN 전용) — cdn.sheetjs.com에서 직접 수신. 라이선스는 Apache-2.0
  (MIT 호환 허용 조건, 재배포 사본 유지 의무 없음 — 참고용으로만 표기).
- 업그레이드 시 `node --check <파일>` 파싱 확인 + exporter.js 전역 계약(`html2canvas`·`XLSX`·`PptxGenJS`) 재검증.
