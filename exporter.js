/* ============================================================================
 * exporter.js — 지먹 v3 브라우저 내보내기 모듈 (플랜 jimeok-v3-map-export T14)
 * ----------------------------------------------------------------------------
 * 정본(map_leaflet.html) T9 export 버튼이 가드로 부르는 단일 계약:
 *   window.JIMEOK_EXPORT(kind, ctx)
 *   kind = png | board | excel | csv | table | pptx | save  (#cmpExportBar button[data-x])
 *   ctx  = { lawd, selection:[{i,name,color,order}], stats(cmpLastStats),
 *            options:{periodMonths,excludeDirect}, generatedAt }
 *
 * 원칙:
 *  - vendor 3종(html2canvas·SheetJS·pptxgenjs) 로컬 전역만 소비 — CDN 런타임 0.
 *  - 통계 재계산 금지 — ctx.stats(T9 computeCompetitorStats 결과)만 소비.
 *    (csv·타입상세의 "원시 거래 나열"은 통계 산식이 아닌 데이터 표시)
 *  - 캡처 가드: 비CORS 베이스맵 차단(T10 setReportMode 가드 재확인) +
 *    캡처 모드 아니면 진입 시도, 거부되면 토스트 후 중단.
 *  - pptx 좌표 변환 = T11 export_deliverables.py 동일 공식:
 *      px = (lng−w)/(e−w)×img_w,  py = (n−lat)/(n−s)×img_h
 *      m_per_px = 111320·cos(lat)·(e−w)/img_w,  반경px = 반경m / m_per_px
 *      16:9 cover 핏 k = max(SW/img_w, SH/img_h), SW=13.333in SH=7.5in
 *  - 전부 try/catch + 실패 토스트(정본 showToast 재사용).
 * ========================================================================== */
(function () {
  "use strict";
  if (typeof window.JIMEOK_EXPORT === "function") return;   // 중복 로드 방지

  var CMP_RADIUS_M = 300;                 // 스펙 §4 경쟁 링 반경 (T11 동일)
  var SITE_RADII_M = [500, 1000, 2000];   // 사업지 반경원 3개 (T11 동일)
  var M_PER_DEG_LAT = 111320;             // T11 박제 상수 — 임의 변경 금지

  // ── 정본 lexical 바인딩 접근 (메인 스크립트 top-level let/const = 전역 어휘 환경 공유) ──
  function mapInst() { try { return map; } catch (e) { return null; } }
  function baseKeyNow() { try { return baseKey; } catch (e) { return null; } }
  function basemaps() { try { return BASEMAPS; } catch (e) { return null; } }
  function md() { return window.MAP_DATA || null; }

  // ── 공용 유틸 ──
  function toast(msg) {
    try {
      if (typeof window.showToast === "function") { window.showToast(msg); return; }
      var t = document.getElementById("toast");
      if (t) {
        t.textContent = msg; t.style.display = "block";
        setTimeout(function () { t.style.display = "none"; }, 2600);
        return;
      }
    } catch (e) { /* 폴백 */ }
    console.warn("[exporter]", msg);
  }
  function settle(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function stamp() {    // %Y%m%d_%H%M%S — T11 ts 동일 계열
    var d = new Date();
    function p(n) { return String(n).padStart(2, "0"); }
    return "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "_" +
      p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }
  function lawdOf(ctx) {
    var D = md();
    return (ctx && ctx.lawd) || (D && D.meta && D.meta.lawd) || "root";
  }
  function srcLine(ctx) {
    var D = md();
    return "출처: 국토교통부 실거래가 (기준 " +
      ((ctx && ctx.generatedAt) || (D && D.meta && D.meta.generated_at) || "") + ")";
  }
  function r1(x) { return Math.floor(x * 10 + 0.5) / 10; }   // half-up 소수1자리 (T9/T11 동일)
  function fmt1(x) {
    if (x == null || !isFinite(x)) return "—";
    var v = r1(x).toFixed(1).split(".");
    return v[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + v[1];
  }
  function fmtInt(x) { return x == null ? "—" : String(Math.round(x)).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
  function typeLabel(k) { return k + "㎡ (≈" + Math.floor(k * 0.3025 + 0.5) + "평)"; }  // T9 typeLabel 동일
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (m) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }
  function hexOf(c) {   // "#RGB"/"#RRGGBB" → 6자리 대문자 hex(없으면 기본색)
    var s = String(c || "").trim();
    if (/^#[0-9a-fA-F]{6}$/.test(s)) return s.slice(1).toUpperCase();
    if (/^#[0-9a-fA-F]{3}$/.test(s)) return (s[1] + s[1] + s[2] + s[2] + s[3] + s[3]).toUpperCase();
    return "0071E3";
  }

  // ── 다운로드 ──
  function downloadBlob(blob, name) {
    var a = document.createElement("a");
    var url = URL.createObjectURL(blob);
    a.href = url; a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  }
  function downloadText(text, name, mime) {
    downloadBlob(new Blob([text], { type: mime || "text/plain;charset=utf-8" }), name);
  }
  function canvasToBlob(canvas) {
    return new Promise(function (resolve, reject) {
      try {
        canvas.toBlob(function (b) { b ? resolve(b) : reject(new Error("canvas→blob 변환 실패")); }, "image/png");
      } catch (e) { reject(e); }
    });
  }

  // ── 캡처 가드 (T10 재확인) ──
  function inReportMode() { return document.body.classList.contains("report-mode"); }
  function ensureCaptureMode() {
    if (!inReportMode()) {
      if (typeof window.setReportMode === "function") {
        try { window.setReportMode(true); } catch (e) { /* 무시 — 아래에서 재확인 */ }
      }
      if (!inReportMode()) return false;   // T10 가드 거부(비CORS) — 정본이 이미 토스트+제안바 표시
    }
    // 방어적 재확인: report-mode 중에도 베이스맵이 비CORS면 차단 (진입 후 수동 전환 케이스)
    var bms = basemaps(), key = baseKeyNow();
    if (bms && key && bms[key] && bms[key].cors === false) {
      toast("캡처는 표준·다크·미니멀·위성에서만");
      return false;
    }
    return true;
  }

  // ── 지도 캡처 공통 (bounds·컨테이너 px 기록 — pptx 좌표 변환용) ──
  async function captureMap() {
    if (typeof window.html2canvas !== "function") throw new Error("html2canvas 미로드");
    var el = document.getElementById("map");
    if (!el) throw new Error("#map 요소 없음");
    await settle(140);   // report-mode invalidateSize(60ms)+아이콘 스케일 정착 대기
    var m = mapInst(), bounds = null;
    if (m && typeof m.getBounds === "function") {
      try {
        var b = m.getBounds();
        bounds = { n: b.getNorth(), s: b.getSouth(), e: b.getEast(), w: b.getWest() };
      } catch (e) { bounds = null; }
    }
    // 캡처 전용 UI 노이즈 일시 숨김 (#reportToast 진입 안내 — #map 자식이라 캡처에 섞임)
    var rt = document.getElementById("reportToast"), rtPrev = null;
    if (rt && rt.style.display !== "none") { rtPrev = rt.style.display; rt.style.display = "none"; }
    var canvas;
    try {
      canvas = await window.html2canvas(el, { useCORS: true, scale: 2, backgroundColor: "#ffffff", logging: false });
    } finally {
      if (rt && rtPrev != null) rt.style.display = rtPrev;
    }
    return { canvas: canvas, bounds: bounds, cw: el.clientWidth, ch: el.clientHeight };
  }

  function needStats(ctx) {
    var st = ctx && ctx.stats;
    if (!st || !st.apts || !st.apts.length) {
      toast("통계 없음 — 단지 선택 후 통계 패널을 열어 다시 시도");
      return null;
    }
    return st;
  }

  // ── ① png: 캡처 모드 #map 그대로 ──
  async function doPng(ctx) {
    if (!ensureCaptureMode()) return;
    var cap = await captureMap();
    var blob = await canvasToBlob(cap.canvas);
    downloadBlob(blob, "지먹_" + lawdOf(ctx) + "_" + stamp() + ".png");
    toast("PNG 다운로드");
  }

  // ── ② board: 지도 캡처 + 통계표 나란히 (임시 컨테이너 → html2canvas) ──
  function boardHtml(ctx, st, mapDataUrl) {
    var D = md(), meta = (D && D.meta) || {};
    var region = meta.region || lawdOf(ctx);
    var apts = (D && D.apts) || [];
    var chips = [
      st.periodStart + " ~ " + st.maxDate + " (" + st.periodMonths + "개월)",
      st.excludeDirect ? "직거래 제외" : "직거래 포함",
      "기준 " + (ctx.generatedAt || meta.generated_at || "")
    ].map(function (t) { return '<span class="jx-chip">' + esc(t) + "</span>"; }).join("");
    var trs = "";
    st.apts.forEach(function (a) {
      var first = true;
      var dong = (apts[a.i] && apts[a.i].dong) || "";
      a.rows.forEach(function (r) {
        var pk = r.peak, at = r.alltime, aptCell = "";
        if (first) {
          first = false;
          aptCell = '<td class="jx-apt" rowspan="' + a.rows.length + '">' +
            '<span class="jx-dot" style="background:' + esc(a.color) + '"></span>' +
            "<div><b>" + esc(a.name) + "</b><span class='jx-sub'>" + esc(dong) +
            (dong ? " · " : "") + "window " + a.winTotal + "건</span></div></td>";
        }
        trs += "<tr>" + aptCell +
          "<td>" + esc(r.type) + "</td>" +
          '<td class="jx-num">' + (r.avg == null ? "표본 없음" : fmt1(r.avg)) + "</td>" +
          '<td class="jx-num">' + (r.median == null ? "표본 없음" : fmt1(r.median)) + "</td>" +
          '<td class="jx-num">' + fmtInt(r.count) + "</td>" +
          '<td class="jx-num">' + (pk ? fmtInt(pk.amt) : "—") + "</td>" +
          "<td>" + (pk ? esc(pk.date) : "—") + "</td>" +
          '<td class="jx-num">' + (at ? fmtInt(at.amt) : "—") + "</td></tr>";
      });
    });
    return '<div class="jx-root">' +
      "<style>" +
      ".jx-root{width:1496px;padding:32px;background:#F4F6F8;color:#1A2230;" +
      "font-family:'Pretendard','Apple SD Gothic Neo','-apple-system',sans-serif;box-sizing:border-box}" +
      ".jx-root *{box-sizing:border-box}" +
      ".jx-head h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0}" +
      ".jx-chips{margin-top:8px}" +
      ".jx-chip{display:inline-block;background:#E8F1FC;color:#0071E3;font-weight:600;font-size:12px;" +
      "padding:4px 10px;border-radius:99px;margin-right:6px}" +
      ".jx-board{display:flex;gap:24px;align-items:stretch;margin-top:20px}" +
      ".jx-map{flex:0 0 860px}" +
      ".jx-map img{width:860px;border-radius:14px;display:block}" +
      ".jx-panel{flex:1;background:#FFFFFF;border-radius:14px;padding:24px;display:flex;flex-direction:column}" +
      ".jx-panel table{width:100%;border-collapse:collapse;font-size:12.5px}" +
      ".jx-panel th{text-align:left;color:#8A94A6;font-size:11px;font-weight:600;padding:6px 8px;" +
      "border-bottom:1px solid #E4E8EE;white-space:nowrap}" +
      ".jx-panel td{padding:8px;border-bottom:1px solid #EEF1F5;vertical-align:middle}" +
      ".jx-num{text-align:right;font-variant-numeric:tabular-nums}" +
      ".jx-apt{width:190px}" +
      ".jx-apt b{font-size:13px;display:block}" +
      ".jx-sub{font-size:11px;color:#8A94A6}" +
      ".jx-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:middle}" +
      ".jx-foot{margin-top:14px;padding-top:14px;font-size:11px;color:#8A94A6}" +
      "</style>" +
      '<div class="jx-head"><h1>경쟁단지 비교 — ' + esc(region) + " (" + esc(lawdOf(ctx)) + ")</h1>" +
      '<div class="jx-chips">' + chips + "</div></div>" +
      '<div class="jx-board">' +
      '<div class="jx-map"><img src="' + mapDataUrl + '" alt="map"></div>' +
      '<div class="jx-panel"><table>' +
      "<thead><tr><th>단지</th><th>타입</th><th>평당가 평균</th><th>중위</th><th>거래량</th>" +
      "<th>최고점(만원)</th><th>최고점 날짜</th><th>역대 최고(만원)</th></tr></thead>" +
      "<tbody>" + trs + "</tbody></table>" +
      '<div class="jx-foot">평당가 단위: 만원/평 · 표본 &lt;3건 타입은 "기타" 합산 · ' +
      "역대 최고 = 기간 무관 전체 데이터 최고점<br>" + esc(srcLine(ctx)) + "</div></div>" +
      "</div></div>";
  }
  async function doBoard(ctx) {
    var st = needStats(ctx); if (!st) return;
    if (typeof window.html2canvas !== "function") throw new Error("html2canvas 미로드");
    if (!ensureCaptureMode()) return;
    var cap = await captureMap();
    var host = document.createElement("div");
    host.style.cssText = "position:fixed;left:-10000px;top:0;z-index:-1;";
    host.innerHTML = boardHtml(ctx, st, cap.canvas.toDataURL("image/png"));
    document.body.appendChild(host);
    try {
      var inner = host.firstElementChild;
      await settle(80);
      var canvas = await window.html2canvas(inner, { useCORS: true, scale: 2, backgroundColor: "#F4F6F8", logging: false });
      var blob = await canvasToBlob(canvas);
      downloadBlob(blob, "지먹_" + lawdOf(ctx) + "_보드_" + stamp() + ".png");
      toast("보드 다운로드");
    } finally { host.remove(); }
  }

  // ── ③ excel: SheetJS 워크북 3시트 (T11 write_xlsx·fill_type_detail 동일 구조) ──
  function doExcel(ctx) {
    if (typeof window.XLSX === "undefined") throw new Error("SheetJS 미로드");
    var st = needStats(ctx); if (!st) return;
    var XLSX = window.XLSX;
    var D = md(), meta = (D && D.meta) || {};
    var trades = (D && D.trades) || [];
    var wb = XLSX.utils.book_new();

    // 시트1: 경쟁단지 비교 (단지×타입 행 — 수치는 ctx.stats만 소비)
    var aoa1 = [["단지", "타입", "평당가 평균(만원/평)", "중위(만원/평)", "거래량",
      "최고점(만원)", "최고점 날짜", "역대 최고(만원)"]];
    st.apts.forEach(function (a) {
      a.rows.forEach(function (r) {
        aoa1.push([a.name, r.type,
          r.avg == null ? "표본 없음" : r1(r.avg),
          r.median == null ? "표본 없음" : r1(r.median),
          r.count,
          r.peak ? r.peak.amt : null, r.peak ? r.peak.date : null,
          r.alltime ? r.alltime.amt : null]);
      });
    });
    var ws1 = XLSX.utils.aoa_to_sheet(aoa1);
    ws1["!cols"] = [{ wch: 28 }, { wch: 16 }, { wch: 18 }, { wch: 16 }, { wch: 9 }, { wch: 13 }, { wch: 13 }, { wch: 15 }];
    XLSX.utils.book_append_sheet(wb, ws1, "경쟁단지 비교");

    // 시트2: 메타 (T11 meta_rows 동일 계열)
    var direct = st.excludeDirect ? "ON (dr=1 전 집계 제외)" : "OFF";
    var aoa2 = [
      ["출처", "국토교통부 실거래가 (RTMS 매매)"],
      ["기준일", (ctx.generatedAt || meta.generated_at || "") + " (번들 meta.generated_at)"],
      ["지역", (meta.region || lawdOf(ctx)) + " (" + lawdOf(ctx) + ")"],
      ["기간", st.periodStart + " ~ " + st.maxDate + " (" + st.periodMonths + "개월, 시작일 비포함)"],
      ["직거래 제외", direct],
      ["중복 제거 방식", "8필드 UNIQUE 내장 (db.py idx_trade_unique_v2) — 수집 단계 적용, 통계 단계 별도 dedup 없음"],
      ["산식 기준", "_디자인기획_지도디테일_v3.md §6 (T9 computeCompetitorStats 결과 소비)"],
      ["선택 단지", st.apts.map(function (a) { return a.order + "." + a.name; }).join(", ")],
      ["생성 시각", new Date().toISOString().slice(0, 19).replace("T", " ")],
      ["생성 도구", "exporter.js (플랜 jimeok-v3 T14 · 브라우저)"]
    ];
    var ws2 = XLSX.utils.aoa_to_sheet(aoa2);
    ws2["!cols"] = [{ wch: 14 }, { wch: 110 }];
    XLSX.utils.book_append_sheet(wb, ws2, "메타");

    // 시트3: 타입 상세 (window 내 원시 거래 나열 — 직거래 플래그·집계 포함 표시, T11 fill_type_detail 동일)
    var aoa3 = [["단지", "타입", "계약일", "전용㎡", "평", "금액(만원)", "층",
      "평당가(만원/평)", "직거래", "집계 포함"]];
    st.apts.forEach(function (a) {
      var rows = trades.filter(function (t) {
        return t && t.a === a.i && t.d && t.amt != null && t.ar != null && t.py &&
          t.d > st.periodStart && t.d <= st.maxDate;
      });
      rows.sort(function (x, y) { return x.d < y.d ? 1 : x.d > y.d ? -1 : 0; });   // DESC (T11 동일)
      var cnt = {};   // 타입별 표본 수 (직거래 제외 후) — "집계 포함" 판정용
      rows.forEach(function (t) {
        if (!(st.excludeDirect && t.dr === 1)) {
          var k = Math.floor(t.ar);
          cnt[k] = (cnt[k] || 0) + 1;
        }
      });
      rows.forEach(function (t) {
        var k = Math.floor(t.ar);
        var isDirect = t.dr === 1;
        var included = !(st.excludeDirect && isDirect) && (cnt[k] || 0) >= 3;
        aoa3.push([a.name, typeLabel(k), t.d, t.ar, t.py, t.amt, t.fl,
          r1(t.amt / t.py), isDirect ? "Y" : "", included ? "Y" : "N"]);
      });
    });
    var ws3 = XLSX.utils.aoa_to_sheet(aoa3);
    ws3["!cols"] = [{ wch: 28 }, { wch: 14 }, { wch: 12 }, { wch: 10 }, { wch: 8 }, { wch: 12 }, { wch: 6 }, { wch: 16 }, { wch: 8 }, { wch: 10 }];
    XLSX.utils.book_append_sheet(wb, ws3, "타입 상세");

    XLSX.writeFile(wb, "경쟁단지_" + lawdOf(ctx) + "_" + stamp() + ".xlsx");
    toast("Excel 다운로드");
  }

  // ── ④ csv: 선택 단지 window 원시 거래 전체 (직거래 포함·플래그 표시 — T11 write_csv 동일) ──
  function csvCell(v) {
    if (v == null) return "";
    var s = String(v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function doCsv(ctx) {
    var st = needStats(ctx); if (!st) return;   // window 범위 = stats 기준 (재계산 없음)
    var sel = (ctx.selection || []).slice().sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    if (!sel.length) { toast("선택된 단지 없음"); return; }
    var D = md(), apts = (D && D.apts) || [], trades = (D && D.trades) || [];
    var lines = [["단지", "법정동", "계약일", "전용㎡", "평", "금액만원", "층", "직거래"]];
    sel.forEach(function (c) {
      var apt = apts[c.i] || {};
      var rows = trades.filter(function (t) {
        return t && t.a === c.i && t.d && t.d > st.periodStart && t.d <= st.maxDate;
      });
      rows.sort(function (x, y) { return x.d > y.d ? 1 : x.d < y.d ? -1 : 0; });   // ASC (T11 동일)
      rows.forEach(function (t) {
        lines.push([apt.name || c.name, apt.dong || "", t.d,
          t.ar == null ? "" : t.ar, t.py == null ? "" : t.py,
          t.amt == null ? "" : t.amt, t.fl == null ? "" : t.fl,
          t.dr === 1 ? "Y" : ""]);
      });
    });
    var text = "\uFEFF" + lines.map(function (r) { return r.map(csvCell).join(","); }).join("\r\n");
    downloadText(text, "경쟁단지_" + lawdOf(ctx) + "_" + stamp() + "_원시거래.csv", "text/csv;charset=utf-8");
    toast("CSV 다운로드 (" + (lines.length - 1) + "건)");
  }

  // ── ⑤ table: 비교표 HTML (인쇄 안전: 솔리드 hex만·shadow 금지·weight≤600) → 클립보드+파일 ──
  function tableHtml(ctx, st) {
    var D = md(), meta = (D && D.meta) || {};
    var region = meta.region || lawdOf(ctx);
    var chips = [
      st.periodStart + " ~ " + st.maxDate + " (" + st.periodMonths + "개월)",
      st.excludeDirect ? "직거래 제외" : "직거래 포함",
      "기준 " + (ctx.generatedAt || meta.generated_at || "")
    ].map(function (t) { return '<span class="chip">' + esc(t) + "</span>"; }).join("");
    var trs = "";
    st.apts.forEach(function (a) {
      var first = true;
      a.rows.forEach(function (r) {
        var pk = r.peak, at = r.alltime, aptCell = "";
        if (first) {
          first = false;
          aptCell = '<td class="apt" rowspan="' + a.rows.length + '">' +
            '<span class="dot" style="background:' + esc(a.color) + '"></span>' +
            "<div><b>" + esc(a.name) + "</b><span class='sub'>window " + a.winTotal + "건</span></div></td>";
        }
        trs += "<tr>" + aptCell +
          "<td>" + esc(r.type) + "</td>" +
          '<td class="num">' + (r.avg == null ? "표본 없음" : fmt1(r.avg)) + "</td>" +
          '<td class="num">' + (r.median == null ? "표본 없음" : fmt1(r.median)) + "</td>" +
          '<td class="num">' + fmtInt(r.count) + "</td>" +
          '<td class="num">' + (pk ? fmtInt(pk.amt) : "—") + "</td>" +
          "<td>" + (pk ? esc(pk.date) : "—") + "</td>" +
          '<td class="num">' + (at ? fmtInt(at.amt) : "—") + "</td></tr>";
      });
    });
    return "<!DOCTYPE html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">" +
      "<title>경쟁단지 비교표 — " + esc(region) + "</title><style>\n" +
      "  * { margin:0; padding:0; box-sizing:border-box; }\n" +
      "  body { font-family:'Pretendard','Apple SD Gothic Neo','-apple-system',sans-serif;\n" +
      "         background:#FFFFFF; color:#1A2230; padding:32px; }\n" +
      "  h1 { font-size:22px; font-weight:600; letter-spacing:-.02em; }\n" +
      "  .chips { margin-top:8px; }\n" +
      "  .chip { display:inline-block; background:#E8F1FC; color:#0071E3; font-weight:600;\n" +
      "          font-size:12px; padding:4px 10px; border-radius:99px; margin-right:6px; }\n" +
      "  table { width:100%; border-collapse:collapse; font-size:12.5px; margin-top:16px; }\n" +
      "  th { text-align:left; color:#5A6472; font-size:11px; font-weight:600; padding:6px 8px;\n" +
      "       border-bottom:2px solid #1A2230; white-space:nowrap; }\n" +
      "  td { padding:8px; border-bottom:1px solid #E4E8EE; vertical-align:middle; font-weight:400; }\n" +
      "  td.num { text-align:right; font-variant-numeric:tabular-nums; }\n" +
      "  td.apt { width:190px; }\n" +
      "  td.apt b { font-size:13px; font-weight:600; display:block; }\n" +
      "  td.apt .sub { font-size:11px; color:#5A6472; }\n" +
      "  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px;\n" +
      "         vertical-align:middle; }\n" +
      "  .foot { margin-top:14px; font-size:11px; color:#5A6472; }\n" +
      "</style></head><body>\n" +
      "<h1>경쟁단지 비교 — " + esc(region) + " (" + esc(lawdOf(ctx)) + ")</h1>\n" +
      '<div class="chips">' + chips + "</div>\n" +
      "<table>\n<thead><tr><th>단지</th><th>타입</th><th style='text-align:right'>평당가 평균(만원/평)</th>" +
      "<th style='text-align:right'>중위(만원/평)</th><th style='text-align:right'>거래량</th>" +
      "<th style='text-align:right'>최고점(만원)</th><th>최고점 날짜</th>" +
      "<th style='text-align:right'>역대 최고(만원)</th></tr></thead>\n" +
      "<tbody>" + trs + "</tbody></table>\n" +
      '<div class="foot">평당가 단위: 만원/평 · 표본 &lt;3건 타입은 "기타" 합산 · ' +
      "역대 최고 = 기간 무관 전체 데이터 최고점<br>" + esc(srcLine(ctx)) + "</div>\n" +
      "</body></html>";
  }
  function doTable(ctx) {
    var st = needStats(ctx); if (!st) return;
    var html = tableHtml(ctx, st);
    var name = "지먹_" + lawdOf(ctx) + "_비교표_" + stamp() + ".html";
    var save = function (clipOk) {
      downloadText(html, name, "text/html;charset=utf-8");
      toast(clipOk ? "비교표 클립보드 복사 + 다운로드" : "클립보드 불가 — 파일만 다운로드");
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(html).then(function () { save(true); }, function () { save(false); });
    } else { save(false); }
  }

  // ── ⑥ pptx: 16:9 배경+링+번호+사업지 핀+반경원+출처 (T11 write_pptx 동일 공식) ──
  async function doPptx(ctx) {
    if (typeof window.PptxGenJS !== "function") throw new Error("pptxgenjs 미로드");
    var st = ctx && ctx.stats;   // 링·핀은 selection만으로 가능 — 기간 칩은 있으면 표시
    if (!ensureCaptureMode()) return;
    var cap = await captureMap();
    var dataUrl = cap.canvas.toDataURL("image/png");
    var img_w = cap.canvas.width, img_h = cap.canvas.height;
    var D = md(), meta = (D && D.meta) || {}, apts = (D && D.apts) || [];

    var p = new window.PptxGenJS();
    p.defineLayout({ name: "JIMEOK_169", width: 13.333, height: 7.5 });   // T11 SW·SH 동일
    p.layout = "JIMEOK_169";
    var slide = p.addSlide();
    var SW = 13.333, SH = 7.5;
    // cover 핏 (넘치는 부분은 슬라이드 밖) — T11 k 공식 동일
    var k = Math.max(SW / img_w, SH / img_h);
    var pw = img_w * k, ph = img_h * k;
    var left = (SW - pw) / 2, top = (SH - ph) / 2;
    slide.addImage({ data: dataUrl, x: left, y: top, w: pw, h: ph });

    var b = cap.bounds;
    if (b && b.n != null && b.s != null && b.e != null && b.w != null && b.e !== b.w && b.n !== b.s) {
      var sel = (ctx.selection || []).slice().sort(function (x, y) { return (x.order || 0) - (y.order || 0); });
      sel.forEach(function (c) {
        var apt = apts[c.i];
        if (!apt || apt.lat == null || apt.lng == null) return;
        // lat/lng → 픽셀 선형 변환 (캡처 시점 bounds 기준 — T11 공식 박제)
        var px = (apt.lng - b.w) / (b.e - b.w) * img_w;
        var py = (b.n - apt.lat) / (b.n - b.s) * img_h;
        // 미터 → 픽셀: 경도 1도 = 111320·cos(lat) m (T11 m_per_px 동일)
        var mPerPx = M_PER_DEG_LAT * Math.cos(apt.lat * Math.PI / 180) * (b.e - b.w) / img_w;
        var r_in = (CMP_RADIUS_M / mPerPx) * k;
        var cx = left + px * k, cy = top + py * k;
        var col = hexOf(c.color);
        slide.addShape(p.shapes.OVAL, { x: cx - r_in, y: cy - r_in, w: 2 * r_in, h: 2 * r_in,
          fill: { type: "none" }, line: { color: col, width: 2.25 } });
        // 번호 배지: 지름 20px 원 · 배경 단지색 · 흰 숫자 · 링 상단 (T11 동일)
        var d = 20 * k;
        slide.addText(String(c.order), { shape: p.shapes.OVAL, x: cx - d / 2, y: cy - r_in - d / 2,
          w: d, h: d, fill: { color: col }, fontSize: 8.5, bold: true, color: "FFFFFF",
          align: "center", valign: "middle" });
      });
      // 사업지 핀 + 반경원 3개 — meta.site 존재 시 (region 번들은 null → 스킵)
      var site = meta.site;
      if (site && site.lat != null && site.lng != null) {
        var spx = (site.lng - b.w) / (b.e - b.w) * img_w;
        var spy = (b.n - site.lat) / (b.n - b.s) * img_h;
        var smPerPx = M_PER_DEG_LAT * Math.cos(site.lat * Math.PI / 180) * (b.e - b.w) / img_w;
        var scx = left + spx * k, scy = top + spy * k;
        SITE_RADII_M.forEach(function (rm) {
          var sr = (rm / smPerPx) * k;
          slide.addShape(p.shapes.OVAL, { x: scx - sr, y: scy - sr, w: 2 * sr, h: 2 * sr,
            fill: { type: "none" }, line: { color: "5A6472", width: 1 } });
        });
        var pd = 16 * k;
        slide.addShape(p.shapes.OVAL, { x: scx - pd / 2, y: scy - pd / 2, w: pd, h: pd,
          fill: { color: "0071E3" }, line: { color: "FFFFFF", width: 1.5 } });
      }
    } else {
      toast("바운드 미확보 — 링 없이 지도만 배치");
    }

    // 제목 (좌상단 — T11 title_box 동일)
    var runs = [{ text: (meta.region || lawdOf(ctx)) + " 경쟁단지 지도",
      options: { fontSize: 18, bold: true, color: "1A2230", breakLine: true } }];
    if (st) runs.push({ text: st.periodStart + " ~ " + st.maxDate + " (" + st.periodMonths + "개월) · " +
      (st.excludeDirect ? "직거래 제외" : "직거래 포함"),
      options: { fontSize: 11, color: "5A6472" } });
    slide.addText(runs, { x: 0.3, y: 0.25, w: 5.2, h: 0.7, valign: "top" });
    // 출처 (우하단 — 정본 문구)
    slide.addText(srcLine(ctx), { x: SW - 5.0, y: SH - 0.62, w: 4.7, h: 0.4,
      align: "right", fontSize: 10, color: "5A6472" });

    await p.writeFile({ fileName: "지먹_" + lawdOf(ctx) + "_경쟁지도_" + stamp() + ".pptx" });
    toast("PPT 다운로드");
  }

  // ── ⑦ save: 선택 JSON (T11 --selection 입력 포맷 [{i,name,color,order}]) ──
  function doSave(ctx) {
    var sel = (ctx.selection || []).map(function (c) {
      return { i: c.i, name: c.name, color: c.color, order: c.order };
    });
    if (!sel.length) { toast("선택된 단지 없음"); return; }
    downloadText(JSON.stringify(sel, null, 2),
      "지먹_" + lawdOf(ctx) + "_selection_" + stamp() + ".json", "application/json;charset=utf-8");
    toast("선택 JSON 다운로드 — export_deliverables.py --selection 입력");
  }

  // ── 디스패치 ──
  var HANDLERS = { png: doPng, board: doBoard, excel: doExcel, csv: doCsv, table: doTable, pptx: doPptx, save: doSave };
  window.JIMEOK_EXPORT = function (kind, ctx) {
    var fn = HANDLERS[kind];
    if (!fn) { toast("알 수 없는 내보내기 종류: " + kind); return; }
    Promise.resolve().then(function () { return fn(ctx || {}); }).catch(function (err) {
      console.error("[exporter]", kind, err);
      toast("내보내기 실패 (" + kind + "): " + ((err && err.message) || err));
    });
  };
  window.JIMEOK_EXPORT.version = "1.0.0 (T14)";

  // 로드 시점 버튼 재평가 — 정본이 태그 직후 호출하지만 순서 변경 대비 방어적 재호출
  if (typeof window.syncCmpExportBtns === "function") window.syncCmpExportBtns();
})();
