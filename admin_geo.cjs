#!/usr/bin/env node
/* admin_geo.js — 좌표 → 행정 위계 3단(시도·시군구·읍면동) 경계
 *
 * 실거래가지도 SGIS 행정경계 레이어용. 사업지 lat/lng를 주면 그 점이 속한
 *   시도(MAP_SIDO) · 시군구(emd dissolve) · 읍면동(emd 개별) 경계를 [lat,lng] 링으로 반환.
 * 데이터: 지도추출기 미러(통계청 SGIS). API키 0.
 *
 * 사용:  node admin_geo.js --lat 37.4863 --lng 126.6709
 * 출력:  stdout JSON {levels:[{level,name,shapes}...], source}
 *   shapes = [ [outerRing, hole...], ... ]  ·  ring = [[lat,lng],...]  (district.js와 동일 포맷)
 */
'use strict';
const fs = require('fs');
const path = require('path');

const MIRROR = path.resolve(__dirname, '../지도추출기_노가다헌터');
const tj = (() => {
  const client = require(path.join(MIRROR, 'vendor/topojson-client.js'));
  const server = require(path.join(MIRROR, 'vendor/topojson-server.js'));
  return Object.assign({}, server, client);
})();

// ---- 미러 mapdata.js 에서 window.MAP_SIDO 로드 ----
function loadSido() {
  const txt = fs.readFileSync(path.join(MIRROR, 'data/mapdata.js'), 'utf8');
  const win = {};
  // 4MB 전체 eval은 무거움 — MAP_SIDO 블록만 잘라 파싱(brace balance)
  const key = 'window.MAP_SIDO=';
  const i = txt.indexOf(key); let j = i + key.length, d = 0, st = j, en = -1;
  for (; j < txt.length; j++) { const c = txt[j]; if (c === '{') d++; else if (c === '}') { if (--d === 0) { en = j + 1; break; } } }
  return JSON.parse(txt.slice(st, en)); // FeatureCollection
}

// ---- 기하 유틸 ----
function sgnArea(r) { let a = 0; for (let i = 0, n = r.length, k = n - 1; i < n; k = i++) a += (r[k][0] * r[i][1]) - (r[i][0] * r[k][1]); return a / 2; }
function rewindGeom(g) {
  if (!g) return g;
  const fix = poly => poly.map((r, i) => ((sgnArea(r) > 0) === (i === 0)) ? r : r.slice().reverse());
  if (g.type === 'Polygon') return { type: 'Polygon', coordinates: fix(g.coordinates) };
  if (g.type === 'MultiPolygon') return { type: 'MultiPolygon', coordinates: g.coordinates.map(fix) };
  return g;
}
function outerRings(g) { // 점-포함 판정용 외곽링들([lng,lat])
  if (!g) return [];
  if (g.type === 'Polygon') return [g.coordinates[0]];
  if (g.type === 'MultiPolygon') return g.coordinates.map(p => p[0]);
  return [];
}
function pip(lng, lat, ring) { let ins = false; for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) { const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1]; if (((yi > lat) !== (yj > lat)) && (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi)) ins = !ins; } return ins; }
function contains(g, lng, lat) { return outerRings(g).some(r => pip(lng, lat, r)); }

// geometry([lng,lat]) → district.js shapes 포맷 [ [outer,hole...], ... ] · ring=[[lat,lng]]
function toShapes(g) {
  const ring = coords => coords.map(c => [Math.round(c[1] * 1e5) / 1e5, Math.round(c[0] * 1e5) / 1e5]);
  if (!g) return [];
  if (g.type === 'Polygon') return [g.coordinates.map(ring)];
  if (g.type === 'MultiPolygon') return g.coordinates.map(poly => poly.map(ring));
  return [];
}
function dissolve(feats) {
  const fs2 = feats.map(f => ({ type: 'Feature', properties: {}, geometry: rewindGeom(f.geometry) }));
  const topo = tj.topology({ g: { type: 'FeatureCollection', features: fs2 } });
  return tj.merge(topo, topo.objects.g.geometries);
}
function emdName(p) { return (p.emdnm || p.emdNm || p.adm_nm || p.EMD_KOR_NM || p.name || '').trim(); }

// ---- 메인 ----
function locate(lat, lng) {
  const out = { levels: [], source: '통계청 SGIS (지도추출기 미러)' };

  // 1) 시도
  const sido = loadSido();
  const sidoF = sido.features.find(f => contains(f.geometry, lng, lat));
  if (!sidoF) throw new Error(`시도 못 찾음 (${lat},${lng}) — 좌표가 한국 밖이거나 경계 데이터 갭`);
  const sidoNm = sidoF.properties.sidonm;
  out.levels.push({ level: '시도', name: sidoNm, shapes: toShapes(sidoF.geometry) });

  // 2) 시군구 후보(같은 시도) 순회 → 점 포함하는 emd 가진 타일 = 그 시군구
  const idx = JSON.parse(fs.readFileSync(path.join(MIRROR, 'data/sgg_name_index.json'), 'utf8'));
  const cands = idx.filter(x => x.sido === sidoNm);
  let sggHit = null, emdHit = null, tileFeats = null;
  for (const c of cands) {
    const tp = path.join(MIRROR, 'data/emd', c.c + '.json');
    if (!fs.existsSync(tp)) continue;
    const tile = JSON.parse(fs.readFileSync(tp, 'utf8'));
    const hit = tile.features.find(f => contains(f.geometry, lng, lat));
    if (hit) { sggHit = c; emdHit = hit; tileFeats = tile.features; break; }
  }
  if (!sggHit) { // 시도까지만이라도 반환(부분 성공)
    process.stderr.write('⚠ 시군구/읍면동 못 찾음 — 시도 경계만 반환\n');
    return out;
  }
  out.levels.push({ level: '시군구', name: sggHit.n, shapes: toShapes(dissolve(tileFeats)) });
  out.levels.push({ level: '읍면동', name: emdName(emdHit.properties) || '(동)', shapes: toShapes(rewindGeom(emdHit.geometry)) });
  return out;
}

function parseArgs(a) { const o = {}; for (let i = 0; i < a.length; i++) if (a[i].startsWith('--')) o[a[i].slice(2)] = a[i + 1]; return o; }
if (require.main === module) {
  const a = parseArgs(process.argv.slice(2));
  try {
    const lat = +a.lat, lng = +a.lng;
    if (!isFinite(lat) || !isFinite(lng)) throw new Error('--lat --lng 필요');
    process.stdout.write(JSON.stringify(locate(lat, lng)));
  } catch (e) { process.stderr.write('❌ ' + e.message + '\n'); process.exit(1); }
}
module.exports = { locate };
