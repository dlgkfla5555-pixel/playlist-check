#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
멜론 플레이리스트 진단 + 추천 웹앱 (경량판, Chrome/Selenium 불필요)
====================================================================

로컬 실행:
    pip install -r requirements.txt
    python app.py

실행 후 자동으로 브라우저가 열립니다 (안 열리면 http://127.0.0.1:5050 직접 접속).

주의: 같은 폴더에 melon_playlist_recommender.py 가 있어야 합니다.
      이 앱은 그 파일의 스크래핑/진단/추천 로직을 그대로 가져다 씁니다.

ANTHROPIC_API_KEY 환경변수가 설정되어 있으면 "추천곡 조회"가 자동으로 API를 호출합니다.
설정되어 있지 않으면, 프롬프트를 복사해서 claude.ai에 붙여넣고 답을 붙여넣는 화면으로
자동 전환됩니다 (API 과금 없이 사용 가능).

클라우드에 무료로 배포하기 (Render 등)
--------------------------------------
Chrome/Selenium이 필요 없는 가벼운 버전이라, Render 무료 플랜(0.1 CPU / 512MB)
에서도 무리 없이 돌아갑니다. 컴퓨터를 꺼도 링크가 계속 살아있습니다.
자세한 배포 방법은 DEPLOY.md를 참고하세요.

공개 링크로 띄울 때 주의할 점
------------------------------
- ANTHROPIC_API_KEY를 설정한 채로 공개하면, 링크를 아는 "누구나"의 추천곡 조회가
  전부 이 키로 과금됩니다. 공개 배포 시엔 이 환경변수를 설정하지 않는 걸 권장합니다
  (모두가 수동/복사-붙여넣기 모드로 동작 → 과금 걱정 없음).
- ACCESS_CODE 환경변수를 설정하면, 그 코드를 모르는 사람은 접속할 수 없습니다
  (아무나 링크를 주워가도 못 쓰게 하는 간단한 잠금장치).
"""

import os
import sys

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import melon_playlist_recommender as core  # noqa: E402

app = Flask(__name__)

ACCESS_CODE = os.environ.get("ACCESS_CODE")  # 설정하면 접속 시 이 코드가 필요함
_COOKIE_NAME = "melon_access"


def _access_granted():
    if not ACCESS_CODE:
        return True
    if request.cookies.get(_COOKIE_NAME) == ACCESS_CODE:
        return True
    if request.args.get("code") == ACCESS_CODE:
        return True
    return False


@app.before_request
def check_access():
    if request.path.startswith("/static"):
        return None
    if _access_granted():
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="접속 코드가 필요합니다."), 403
    return GATE_HTML, 401


@app.after_request
def set_access_cookie(resp):
    if ACCESS_CODE and request.args.get("code") == ACCESS_CODE:
        resp.set_cookie(_COOKIE_NAME, ACCESS_CODE, max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")
    return resp


GATE_HTML = """<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>접속 코드 필요</title>
<style>
  body{font-family:-apple-system,sans-serif;background:#F5F4EF;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}
  .box{background:#fff;border:1px solid #E2E0D6;border-radius:14px;padding:32px;max-width:320px;width:90%;}
  h1{font-size:18px;margin:0 0 12px;}
  input{width:100%;padding:10px 12px;border:1px solid #E2E0D6;border-radius:8px;font-size:14px;box-sizing:border-box;}
  button{margin-top:12px;width:100%;background:#1E1E1C;color:#fff;border:none;border-radius:8px;
         padding:11px;font-size:14px;font-weight:700;cursor:pointer;}
  p{font-size:13px;color:#6B6A63;}
</style></head>
<body><div class="box">
  <h1>접속 코드를 입력해주세요</h1>
  <p>이 페이지는 접속 코드가 있어야 볼 수 있습니다.</p>
  <input type="password" id="code" placeholder="접속 코드" autofocus>
  <button onclick="go()">입장</button>
</div>
<script>
function go(){
  const c = document.getElementById('code').value;
  location.href = location.pathname + '?code=' + encodeURIComponent(c);
}
document.getElementById('code').addEventListener('keydown', e => { if(e.key==='Enter') go(); });
</script>
</body></html>"""


# =========================================================
# API
# =========================================================

@app.route("/api/playlist")
def api_playlist():
    raw = request.args.get("playlist", "").strip()
    with_year = request.args.get("with_year", "false").lower() == "true"
    debug = request.args.get("debug", "false").lower() == "true"
    if not raw:
        return jsonify(ok=False, error="플레이리스트 URL 또는 번호를 입력해주세요."), 400
    try:
        seq = core.extract_plylst_seq(raw)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    try:
        tracks, meta = core.scrape_playlist(
            seq, with_year=with_year, extract_meta_info=True, debug=debug
        )
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"스크래핑 중 오류: {e}"), 500

    if not tracks:
        return jsonify(
            ok=False,
            error="곡 목록을 가져오지 못했습니다. 비공개 플레이리스트이거나 페이지 구조가 달라졌을 수 있습니다.",
        )

    return jsonify(
        ok=True, seq=seq,
        title=meta.get("title"), description=meta.get("description"),
        tracks=tracks, track_count=len(tracks),
    )


@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    data = request.get_json(force=True)
    tracks = data.get("tracks", [])
    concept = data.get("concept") or None
    exclude = _norm_exclude(data.get("exclude"))
    if not tracks:
        return jsonify(ok=False, error="곡 목록이 없습니다. 먼저 플레이리스트를 불러와주세요."), 400
    diag = core.diagnose(tracks, concept=concept, exclude_keywords=exclude)
    return jsonify(ok=True, diagnosis=diag)


@app.route("/api/recommend/prompt", methods=["POST"])
def api_recommend_prompt():
    data = request.get_json(force=True)
    tracks = data.get("tracks", [])
    diag = data.get("diagnosis")
    concept = data.get("concept") or None
    exclude = _norm_exclude(data.get("exclude"))
    n = int(data.get("n", 8))
    if not tracks or not diag:
        return jsonify(ok=False, error="곡 목록/진단 결과가 없습니다. 먼저 '플리 진단'을 실행해주세요."), 400
    prompt = core.build_prompt(tracks, diag, concept, exclude, n)
    return jsonify(ok=True, prompt=prompt)


@app.route("/api/recommend/auto", methods=["POST"])
def api_recommend_auto():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify(ok=False, error="no_api_key")
    data = request.get_json(force=True)
    tracks = data.get("tracks", [])
    diag = data.get("diagnosis")
    concept = data.get("concept") or None
    exclude = _norm_exclude(data.get("exclude"))
    n = int(data.get("n", 8))
    model = data.get("model", "claude-sonnet-5")
    if not tracks or not diag:
        return jsonify(ok=False, error="곡 목록/진단 결과가 없습니다. 먼저 '플리 진단'을 실행해주세요."), 400
    try:
        from anthropic import Anthropic
        client = Anthropic()
        prompt = core.build_prompt(tracks, diag, concept, exclude, n)
        resp = client.messages.create(
            model=model, max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        parsed = core._parse_ai_json(text)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"AI 호출 실패: {e}"), 500
    return jsonify(
        ok=True,
        inferred_genre=parsed.get("inferred_genre"),
        recommendations=parsed.get("recommendations", []),
    )


@app.route("/api/recommend/parse", methods=["POST"])
def api_recommend_parse():
    data = request.get_json(force=True)
    text = data.get("response_text", "")
    try:
        parsed = core._parse_ai_json(text)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"JSON 파싱 실패: {e}. claude.ai 답변에 JSON 외 다른 텍스트가 섞이지 않았는지 확인해주세요."), 400
    return jsonify(
        ok=True,
        inferred_genre=parsed.get("inferred_genre"),
        recommendations=parsed.get("recommendations", []),
    )


@app.route("/api/verify", methods=["POST"])
def api_verify():
    data = request.get_json(force=True)
    recs = data.get("recommendations", [])
    if not recs:
        return jsonify(ok=True, recommendations=[])
    try:
        verified = core.verify_on_melon(recs)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"검증 중 오류: {e}"), 500
    return jsonify(ok=True, recommendations=verified)


def _norm_exclude(exclude):
    if not exclude:
        return []
    if isinstance(exclude, str):
        return [k.strip() for k in exclude.split(",") if k.strip()]
    return [k.strip() for k in exclude if k and k.strip()]


# =========================================================
# Frontend (단일 페이지)
# =========================================================

@app.route("/")
def index():
    return INDEX_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>플레이리스트 진단 + 추천</title>
<style>
  :root{
    --bg: #F5F4EF;
    --panel: #FFFFFF;
    --ink: #1E1E1C;
    --sub: #6B6A63;
    --line: #E2E0D6;
    --good: #0F6E56;
    --good-bg: #E1F5EE;
    --warn: #854F0B;
    --warn-bg: #FAEEDA;
    --bad: #A32D2D;
    --bad-bg: #FCEBEB;
    --mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; background:var(--bg); color:var(--ink);
    font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif;
    line-height:1.55;
  }
  .wrap{max-width:820px;margin:0 auto;padding:48px 24px 80px;}
  header{margin-bottom:28px;}
  .eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--sub);margin:0 0 10px;}
  h1{font-size:26px;font-weight:700;margin:0 0 8px;letter-spacing:-0.01em;}
  .desc{color:var(--sub);font-size:14px;max-width:600px;}

  .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:20px;}
  label{font-size:13px;font-weight:700;color:var(--ink);display:block;margin-bottom:8px;}
  .hint{font-size:12px;color:var(--sub);margin:6px 0 12px;}

  input[type=text], textarea{
    width:100%;border:1px solid var(--line);border-radius:8px;padding:10px 12px;
    font-family:inherit;font-size:14px;background:#FBFBF9;color:var(--ink);
  }
  textarea{font-family:var(--mono);font-size:13px;resize:vertical;}
  input:focus, textarea:focus{outline:2px solid var(--good);outline-offset:1px;}

  .row{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;}
  .row > *{flex:1 1 auto;}
  .row.tight{gap:8px;}

  button{
    background:var(--ink);color:#fff;border:none;border-radius:8px;
    padding:11px 20px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;
  }
  button:hover{opacity:.88;}
  button:disabled{opacity:.4;cursor:not-allowed;}
  button.secondary{background:transparent;color:var(--sub);border:1px solid var(--line);font-weight:600;}
  button.small{padding:7px 12px;font-size:12px;}

  .checkline{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--sub);margin-top:10px;}
  .checkline input{width:auto;}

  .overall{display:flex;align-items:center;justify-content:space-between;gap:16px;}
  .overall .num{font-size:44px;font-weight:800;font-family:var(--mono);}
  .overall .label{font-size:13px;color:var(--sub);}

  .metric{margin-bottom:18px;}
  .metric:last-child{margin-bottom:0;}
  .metric-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;}
  .metric-title{font-size:14px;font-weight:700;}
  .metric-score{font-family:var(--mono);font-size:13px;font-weight:700;}

  .meter{height:8px;border-radius:4px;background:var(--line);overflow:hidden;display:flex;margin-bottom:8px;}
  .meter-fill{height:100%;border-radius:4px;}

  .note-list{margin:8px 0 0;padding:0;list-style:none;font-size:13px;}
  .note-list li{padding:8px 10px;border-radius:6px;margin-bottom:6px;display:flex;gap:8px;}
  .note-list li .tag{font-family:var(--mono);font-size:11px;font-weight:700;white-space:nowrap;}
  .tier-good{background:var(--good-bg);color:var(--good);}
  .tier-warn{background:var(--warn-bg);color:var(--warn);}
  .tier-bad{background:var(--bad-bg);color:var(--bad);}

  .stat-row{display:flex;gap:16px;flex-wrap:wrap;margin-top:4px;font-size:12px;color:var(--sub);}

  table{width:100%;border-collapse:collapse;font-size:13px;}
  th, td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);}
  th{color:var(--sub);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.04em;}
  .tracklist{max-height:260px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;}

  .rec-card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start;}
  .rec-card input[type=checkbox]{margin-top:4px;width:auto;}
  .rec-main{flex:1;}
  .rec-title{font-weight:700;font-size:14px;margin-bottom:4px;}
  .rec-reason{font-size:13px;color:var(--sub);margin-bottom:6px;}
  .badge{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-right:6px;}
  .badge.ok{background:var(--good-bg);color:var(--good);}
  .badge.warn{background:var(--warn-bg);color:var(--warn);}
  .rec-links a{font-size:12px;color:var(--ink);text-decoration:underline;}

  .status{font-size:13px;color:var(--sub);margin-top:10px;}
  .status.err{color:var(--bad);}
  .status.ok{color:var(--good);}

  .spinner{
    display:inline-block;width:14px;height:14px;border:2px solid var(--line);
    border-top-color:var(--ink);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px;
  }
  @keyframes spin{to{transform:rotate(360deg);}}

  .hidden{display:none !important;}
  .genre-box{background:#FBFBF9;border:1px dashed var(--line);border-radius:8px;padding:12px;font-size:13px;margin-bottom:14px;}
  .genre-box b{color:var(--ink);}

  footer{margin-top:28px;font-size:12px;color:var(--sub);}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <p class="eyebrow">Playlist QA + Recommend</p>
    <h1>플레이리스트 진단 + 추천</h1>
    <p class="desc">멜론 플레이리스트 URL 또는 번호를 입력하면 곡 목록을 불러오고, 5개 지표로 진단한 뒤,
    플레이리스트의 주제/장르에 맞는 추천곡까지 한 화면에서 확인할 수 있습니다.</p>
  </header>

  <!-- 1. 플레이리스트 불러오기 -->
  <div class="panel">
    <label for="playlistInput">멜론 플레이리스트 URL 또는 번호</label>
    <div class="row">
      <input type="text" id="playlistInput" placeholder="https://www.melon.com/mymusic/dj/mymusicdjplaylistview_inform.htm?plylstSeq=508316846 또는 508316846">
      <button id="loadBtn" onclick="loadPlaylist()">불러오기</button>
    </div>
    <div class="checkline">
      <input type="checkbox" id="withYear">
      <label for="withYear" style="margin:0;font-weight:400;">발매년도까지 조회 (신선도 분석용, 느려짐)</label>
    </div>
    <div class="checkline">
      <input type="checkbox" id="debugMode">
      <label for="debugMode" style="margin:0;font-weight:400;">디버그 모드 (실패 시 서버 콘솔에 상세 로그 출력)</label>
    </div>
    <div id="loadStatus" class="status"></div>
  </div>

  <!-- 2. 플레이리스트 정보 -->
  <div class="panel hidden" id="infoPanel">
    <div class="row" style="margin-bottom:14px;">
      <div style="flex:2;">
        <label>불러온 플레이리스트</label>
        <div id="plTitle" style="font-weight:700;font-size:15px;margin-bottom:4px;"></div>
        <div id="plCount" class="hint" style="margin:0;"></div>
      </div>
    </div>

    <label for="conceptInput">컨셉 / 장르 (플레이리스트 소개글에서 자동으로 채웠습니다 — 필요하면 더 구체적으로 수정하세요)</label>
    <textarea id="conceptInput" rows="8" placeholder="예: 국내 인디/모던록 밴드 씬, 감성 보컬, 미드템포~업템포, 실제 밴드 편성 사운드"></textarea>
    <p class="hint">세부 장르/씬/보컬스타일/템포/국내외 여부까지 구체적으로 적을수록 추천 정확도가 올라갑니다.</p>

    <label for="excludeInput">피하고 싶은 키워드 (쉼표로 구분, 선택)</label>
    <input type="text" id="excludeInput" placeholder="예: 발라드, 아이돌팝">

    <div class="row" style="margin-top:16px;">
      <div style="flex:1;">
        <label style="margin-bottom:8px;">수록곡 목록</label>
        <div class="tracklist">
          <table>
            <thead><tr><th>#</th><th>곡명</th><th>아티스트</th><th>년도</th></tr></thead>
            <tbody id="trackTableBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="row" style="margin-top:16px;">
      <button onclick="runDiagnose()" id="diagnoseBtn">플리 진단</button>
      <button onclick="runRecommend()" id="recommendBtn" class="secondary">추천곡 조회</button>
    </div>
    <div id="diagnoseStatus" class="status"></div>
  </div>

  <!-- 3. 진단 결과 -->
  <div id="results" class="hidden">
    <div class="panel">
      <div class="overall">
        <div>
          <div class="label">종합 메타데이터 점수</div>
          <div class="num" id="overallScore">-</div>
        </div>
        <div class="label" id="trackCount"></div>
      </div>
      <div id="conceptFlagBox"></div>
    </div>

    <div class="panel"><div class="metric">
      <div class="metric-head"><div class="metric-title">1. 아티스트 다양성</div><div class="metric-score" id="s1score"></div></div>
      <div class="meter"><div class="meter-fill" id="s1bar"></div></div>
      <div class="stat-row" id="s1stats"></div>
      <ul class="note-list" id="s1notes"></ul>
    </div></div>

    <div class="panel"><div class="metric">
      <div class="metric-head"><div class="metric-title">2. 중복/유사 버전</div><div class="metric-score" id="s2score"></div></div>
      <div class="meter"><div class="meter-fill" id="s2bar"></div></div>
      <ul class="note-list" id="s2notes"></ul>
    </div></div>

    <div class="panel"><div class="metric">
      <div class="metric-head"><div class="metric-title">3. Feat. 밀도</div><div class="metric-score" id="s3score"></div></div>
      <div class="meter"><div class="meter-fill" id="s3bar"></div></div>
      <div class="stat-row" id="s3stats"></div>
      <ul class="note-list" id="s3notes"></ul>
    </div></div>

    <div class="panel"><div class="metric">
      <div class="metric-head"><div class="metric-title">4. 구간별 / 인접 아티스트 쏠림</div><div class="metric-score" id="s4score"></div></div>
      <div class="meter"><div class="meter-fill" id="s4bar"></div></div>
      <ul class="note-list" id="s4notes"></ul>
    </div></div>

    <div class="panel"><div class="metric">
      <div class="metric-head"><div class="metric-title">5. 신선도 (발매년도)</div><div class="metric-score" id="s5score"></div></div>
      <div class="meter"><div class="meter-fill" id="s5bar"></div></div>
      <div class="stat-row" id="s5stats"></div>
      <ul class="note-list" id="s5notes"></ul>
    </div></div>
  </div>

  <!-- 4. 추천 -->
  <div id="recommendPanel" class="panel hidden">
    <label style="margin-bottom:14px;">추천곡</label>

    <!-- 수동(API 없음) 모드 -->
    <div id="manualBox" class="hidden">
      <p class="hint">API 키가 설정되어 있지 않아 수동 모드입니다. 아래 프롬프트를 복사해서
      <a href="https://claude.ai" target="_blank">claude.ai</a>에 붙여넣고, 받은 답변(JSON)을 그 아래 칸에 붙여넣어주세요.</p>
      <div class="row tight">
        <button class="secondary small" onclick="copyPrompt()">프롬프트 복사</button>
      </div>
      <textarea id="promptBox" rows="6" readonly style="margin-top:8px;"></textarea>
      <label style="margin-top:14px;">claude.ai 답변 붙여넣기</label>
      <textarea id="responseBox" rows="6" placeholder="여기에 JSON 답변을 붙여넣으세요"></textarea>
      <div class="row" style="margin-top:10px;">
        <button onclick="applyManualResponse()">적용</button>
      </div>
    </div>

    <div id="recommendStatus" class="status"></div>
    <div id="genreBox" class="genre-box hidden"></div>
    <div id="recList"></div>

    <div class="row hidden" id="recActions" style="margin-top:12px;">
      <button class="secondary" onclick="verifyRecs()">멜론에서 실존 검증</button>
      <button onclick="addSelectedAndRediagnose()">선택한 곡 추가 후 재진단</button>
    </div>
  </div>

  <footer>이 페이지는 서버에서 멜론 공개 페이지를 읽어와 곡 목록을 가져옵니다.</footer>
</div>

<script>
const state = {
  seq: null,
  tracks: [],
  diagnosis: null,
  recommendations: [],
};

function setStatus(id, msg, type){
  const el = document.getElementById(id);
  el.className = 'status' + (type ? ' ' + type : '');
  el.innerHTML = msg;
}

function tierClass(score){
  if(score >= 80) return {cls:'tier-good', color:'var(--good)'};
  if(score >= 60) return {cls:'tier-warn', color:'var(--warn)'};
  return {cls:'tier-bad', color:'var(--bad)'};
}
function setMeter(barId, score){
  const t = tierClass(score);
  const bar = document.getElementById(barId);
  bar.style.width = score + '%';
  bar.style.background = t.color;
}
function noteItem(cls, tag, text){
  const li = document.createElement('li');
  li.className = cls;
  li.innerHTML = tag ? `<span class="tag">${tag}</span><span>${text}</span>` : text;
  return li;
}

// ---------- 1. 플레이리스트 불러오기 ----------
async function loadPlaylist(){
  const raw = document.getElementById('playlistInput').value.trim();
  if(!raw){ setStatus('loadStatus', '플레이리스트 URL 또는 번호를 입력해주세요.', 'err'); return; }
  const withYear = document.getElementById('withYear').checked;
  const debugMode = document.getElementById('debugMode').checked;
  document.getElementById('loadBtn').disabled = true;
  setStatus('loadStatus', '<span class="spinner"></span>곡 목록 불러오는 중... (수십 초 걸릴 수 있어요)', '');

  try {
    const res = await fetch(`/api/playlist?playlist=${encodeURIComponent(raw)}&with_year=${withYear}&debug=${debugMode}`);
    const data = await res.json();
    document.getElementById('loadBtn').disabled = false;
    if(!data.ok){
      setStatus('loadStatus', '❌ ' + data.error, 'err');
      return;
    }
    state.seq = data.seq;
    state.tracks = data.tracks;
    setStatus('loadStatus', `✅ ${data.track_count}곡 불러옴`, 'ok');

    document.getElementById('infoPanel').classList.remove('hidden');
    document.getElementById('plTitle').textContent = data.title || '(제목을 가져오지 못했습니다)';
    document.getElementById('plCount').textContent = `총 ${data.track_count}곡`;
    document.getElementById('conceptInput').value = data.description || '';

    const tbody = document.getElementById('trackTableBody');
    tbody.innerHTML = '';
    data.tracks.forEach((t, i) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${i+1}</td><td>${t.title}</td><td>${t.artist}</td><td>${t.year || '-'}</td>`;
      tbody.appendChild(tr);
    });

    document.getElementById('results').classList.add('hidden');
    document.getElementById('recommendPanel').classList.add('hidden');
  } catch(e){
    document.getElementById('loadBtn').disabled = false;
    setStatus('loadStatus', '❌ 요청 실패: ' + e, 'err');
  }
}

function currentConceptExclude(){
  return {
    concept: document.getElementById('conceptInput').value.trim(),
    exclude: document.getElementById('excludeInput').value.trim(),
  };
}

// ---------- 2. 진단 ----------
async function runDiagnose(){
  if(!state.tracks.length) return;
  const {concept, exclude} = currentConceptExclude();
  setStatus('diagnoseStatus', '<span class="spinner"></span>진단 중...', '');
  document.getElementById('diagnoseBtn').disabled = true;
  try {
    const res = await fetch('/api/diagnose', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({tracks: state.tracks, concept, exclude}),
    });
    const data = await res.json();
    document.getElementById('diagnoseBtn').disabled = false;
    if(!data.ok){ setStatus('diagnoseStatus', '❌ ' + data.error, 'err'); return; }
    state.diagnosis = data.diagnosis;
    setStatus('diagnoseStatus', '', '');
    renderDiagnosis(data.diagnosis);
  } catch(e){
    document.getElementById('diagnoseBtn').disabled = false;
    setStatus('diagnoseStatus', '❌ 요청 실패: ' + e, 'err');
  }
}

function renderDiagnosis(d){
  document.getElementById('results').classList.remove('hidden');
  document.getElementById('overallScore').textContent = d.overall;
  document.getElementById('overallScore').style.color = tierClass(d.overall).color;
  document.getElementById('trackCount').textContent = `총 ${d.track_count}곡 분석`;

  const flagBox = document.getElementById('conceptFlagBox');
  flagBox.innerHTML = '';
  if(d.concept_flagged && d.concept_flagged.length){
    const ul = document.createElement('ul');
    ul.className = 'note-list';
    ul.style.marginTop = '12px';
    d.concept_flagged.forEach(f => {
      ul.appendChild(noteItem('tier-warn', '컨셉주의', `${f.artist} - ${f.title} (키워드: ${f.keyword})`));
    });
    flagBox.appendChild(ul);
  }

  // 1. 아티스트 다양성
  const ad = d.artist_diversity;
  document.getElementById('s1score').textContent = ad.score + '/100';
  setMeter('s1bar', ad.score);
  document.getElementById('s1stats').innerHTML =
    `<span>아티스트 수 ${ad.artist_count}명</span><span>전체 곡 수 ${ad.track_count}곡</span><span>상위 5명 점유율 ${ad.top5_share}%</span>`;
  const s1notes = document.getElementById('s1notes'); s1notes.innerHTML = '';
  if(ad.over_represented.length){
    ad.over_represented.forEach(([name,c]) => {
      s1notes.appendChild(noteItem('tier-warn', c+'곡', `${name} — 비중 ${(c/ad.track_count*100).toFixed(1)}%, 신곡 교체 시 감량 후보`));
    });
  } else s1notes.appendChild(noteItem('tier-good', '', '특별히 쏠린 아티스트가 없습니다.'));

  // 2. 중복/유사버전
  const dv = d.duplicate_versions;
  document.getElementById('s2score').textContent = dv.score + '/100';
  setMeter('s2bar', dv.score);
  const s2notes = document.getElementById('s2notes'); s2notes.innerHTML = '';
  if(dv.duplicates.length){
    dv.duplicates.forEach(([artist, titles]) => {
      s2notes.appendChild(noteItem('tier-warn', '중복', `${artist} — ${titles.join(' / ')} : 같은 곡의 다른 버전으로 추정, 1개만 남기는 걸 검토`));
    });
  } else s2notes.appendChild(noteItem('tier-good', '', '같은 아티스트 내 동일 곡 중복 버전은 발견되지 않았습니다.'));

  // 3. Feat. 밀도
  const fd = d.feat_density;
  document.getElementById('s3score').textContent = fd.score + '/100';
  setMeter('s3bar', fd.score);
  document.getElementById('s3stats').innerHTML = `<span>Feat. 포함 곡 ${fd.feat_count}/${d.track_count}곡 (${fd.ratio}%)</span>`;
  const s3notes = document.getElementById('s3notes'); s3notes.innerHTML = '';
  if(fd.repeated_feats.length){
    fd.repeated_feats.forEach(([name,c]) => {
      s3notes.appendChild(noteItem('tier-warn', c+'회', `${name} — 피처링으로 반복 등장`));
    });
  } else s3notes.appendChild(noteItem('tier-good', '', '특정 아티스트에 피처링이 몰려있지 않습니다.'));

  // 4. 구간별/인접 아티스트 쏠림
  const sa = d.section_adjacent;
  document.getElementById('s4score').textContent = sa.score + '/100';
  setMeter('s4bar', sa.score);
  const s4notes = document.getElementById('s4notes'); s4notes.innerHTML = '';
  let anyIssue = false;
  sa.section_issues.forEach(i => {
    anyIssue = true;
    s4notes.appendChild(noteItem('tier-warn', '구간쏠림', `${i.section}(${i.range[0]}~${i.range[1]}번)에 ${i.artist} ${i.count}곡 몰림`));
  });
  if(sa.adjacent_flags.length){
    anyIssue = true;
    s4notes.appendChild(noteItem('tier-warn', '인접반복', `${sa.adjacent_flags.length}건: 4곡 이내 같은 아티스트 반복`));
  }
  if(!anyIssue) s4notes.appendChild(noteItem('tier-good', '', '구간별/인접 아티스트 쏠림이 발견되지 않았습니다.'));

  // 5. 신선도
  const fr = d.freshness;
  const s5stats = document.getElementById('s5stats'); s5stats.innerHTML = '';
  const s5notes = document.getElementById('s5notes'); s5notes.innerHTML = '';
  if(fr){
    document.getElementById('s5score').textContent = fr.score + '/100';
    setMeter('s5bar', fr.score);
    s5stats.innerHTML = `<span>발매년도 확인 ${fr.with_year_count}/${fr.track_count}곡</span><span>연도범위 ${fr.year_range[0]}~${fr.year_range[1]}</span><span>최근 2년 이내 비중 ${fr.recent_share}%</span>`;
    if(fr.score < 70) s5notes.appendChild(noteItem('tier-warn', '', '최근 발매곡 비중이 낮습니다. 최신곡 추가를 고려해보세요.'));
    else s5notes.appendChild(noteItem('tier-good', '', '신선도가 양호합니다.'));
  } else {
    document.getElementById('s5score').textContent = '측정 불가';
    setMeter('s5bar', 0);
    s5notes.appendChild(noteItem('tier-warn', '', '발매년도 데이터가 부족합니다. "발매년도까지 조회"를 켜고 다시 불러와보세요.'));
  }
}

// ---------- 3. 추천 ----------
async function runRecommend(){
  if(!state.diagnosis){ await runDiagnose(); if(!state.diagnosis) return; }
  const {concept, exclude} = currentConceptExclude();
  document.getElementById('recommendPanel').classList.remove('hidden');
  document.getElementById('recActions').classList.add('hidden');
  document.getElementById('genreBox').classList.add('hidden');
  document.getElementById('recList').innerHTML = '';
  setStatus('recommendStatus', '<span class="spinner"></span>추천곡 요청 중...', '');
  document.getElementById('recommendBtn').disabled = true;

  try {
    const res = await fetch('/api/recommend/auto', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({tracks: state.tracks, diagnosis: state.diagnosis, concept, exclude, n: 8}),
    });
    const data = await res.json();
    document.getElementById('recommendBtn').disabled = false;

    if(data.ok === false && data.error === 'no_api_key'){
      setStatus('recommendStatus', 'API 키가 없어 수동 모드로 전환합니다.', '');
      await showManualMode(concept, exclude);
      return;
    }
    if(!data.ok){ setStatus('recommendStatus', '❌ ' + data.error, 'err'); return; }
    setStatus('recommendStatus', '', '');
    applyRecommendations(data.inferred_genre, data.recommendations);
  } catch(e){
    document.getElementById('recommendBtn').disabled = false;
    setStatus('recommendStatus', '❌ 요청 실패: ' + e, 'err');
  }
}

async function showManualMode(concept, exclude){
  document.getElementById('manualBox').classList.remove('hidden');
  const res = await fetch('/api/recommend/prompt', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({tracks: state.tracks, diagnosis: state.diagnosis, concept, exclude, n: 8}),
  });
  const data = await res.json();
  if(!data.ok){ setStatus('recommendStatus', '❌ ' + data.error, 'err'); return; }
  document.getElementById('promptBox').value = data.prompt;
}

function copyPrompt(){
  const box = document.getElementById('promptBox');
  box.select();
  navigator.clipboard.writeText(box.value).then(() => {
    setStatus('recommendStatus', '📋 프롬프트가 복사되었습니다. claude.ai에 붙여넣어주세요.', 'ok');
  });
}

async function applyManualResponse(){
  const text = document.getElementById('responseBox').value.trim();
  if(!text){ setStatus('recommendStatus', '답변을 먼저 붙여넣어주세요.', 'err'); return; }
  setStatus('recommendStatus', '<span class="spinner"></span>파싱 중...', '');
  try {
    const res = await fetch('/api/recommend/parse', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({response_text: text}),
    });
    const data = await res.json();
    if(!data.ok){ setStatus('recommendStatus', '❌ ' + data.error, 'err'); return; }
    setStatus('recommendStatus', '', '');
    applyRecommendations(data.inferred_genre, data.recommendations);
  } catch(e){
    setStatus('recommendStatus', '❌ 요청 실패: ' + e, 'err');
  }
}

function applyRecommendations(genre, recs){
  state.recommendations = recs || [];
  const genreBox = document.getElementById('genreBox');
  if(genre){
    genreBox.classList.remove('hidden');
    genreBox.innerHTML = `<b>🎧 AI가 파악한 사운드 프로파일</b><br>${genre}`;
  }
  renderRecommendations(state.recommendations);
  if(state.recommendations.length){
    document.getElementById('recActions').classList.remove('hidden');
  }
}

function renderRecommendations(recs){
  const list = document.getElementById('recList');
  list.innerHTML = '';
  if(!recs || !recs.length){
    list.innerHTML = '<p class="hint">추천곡이 없습니다.</p>';
    return;
  }
  recs.forEach((r, i) => {
    const card = document.createElement('div');
    card.className = 'rec-card';
    const verified = r.verified === true;
    const verifiedKnown = 'verified' in r;
    const badge = !verifiedKnown ? '' :
      (verified ? '<span class="badge ok">확인됨</span>' : '<span class="badge warn">실존 확인 필요</span>');
    const searchUrl = r.search_url || `https://www.melon.com/search/song/index.htm?q=${encodeURIComponent(r.artist + ' ' + r.title)}`;
    card.innerHTML = `
      <input type="checkbox" class="recCheck" data-idx="${i}" checked>
      <div class="rec-main">
        <div class="rec-title">${badge}${r.artist} - ${r.title}</div>
        <div class="rec-reason">${r.reason || ''}</div>
        <div class="rec-links"><a href="${searchUrl}" target="_blank">멜론에서 확인 →</a></div>
      </div>`;
    list.appendChild(card);
  });
}

async function verifyRecs(){
  setStatus('recommendStatus', '<span class="spinner"></span>멜론에서 실존 여부 확인 중...', '');
  try {
    const res = await fetch('/api/verify', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({recommendations: state.recommendations}),
    });
    const data = await res.json();
    if(!data.ok){ setStatus('recommendStatus', '❌ ' + data.error, 'err'); return; }
    state.recommendations = data.recommendations;
    setStatus('recommendStatus', '', '');
    renderRecommendations(state.recommendations);
  } catch(e){
    setStatus('recommendStatus', '❌ 요청 실패: ' + e, 'err');
  }
}

async function addSelectedAndRediagnose(){
  const checks = document.querySelectorAll('.recCheck:checked');
  const added = [];
  checks.forEach(c => {
    const rec = state.recommendations[parseInt(c.dataset.idx)];
    added.push({title: rec.title, artist: rec.artist, year: null, song_id: null});
  });
  if(!added.length) return;
  state.tracks = state.tracks.concat(added);
  document.getElementById('plCount').textContent = `총 ${state.tracks.length}곡`;
  const tbody = document.getElementById('trackTableBody');
  state.tracks.forEach((t, i) => {
    if(i < tbody.children.length) return;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${i+1}</td><td>${t.title}</td><td>${t.artist}</td><td>${t.year || '-'}</td>`;
    tbody.appendChild(tr);
  });
  await runDiagnose();
  document.getElementById('results').scrollIntoView({behavior:'smooth'});
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # Render 등 클라우드 플랫폼은 PORT 환경변수로 포트를 지정해준다.
    # 로컬에서 그냥 실행할 땐 5050을 기본으로 쓴다.
    PORT = int(os.environ.get("PORT", 5050))
    IS_LOCAL = "PORT" not in os.environ

    print("=" * 60)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY가 설정되어 있습니다.")
        print("    이 서버를 공개 링크로 다른 사람과 공유하면,")
        print("    '추천곡 조회'를 누르는 모든 방문자의 비용이 이 키로 청구됩니다.")
        print("    공개로 돌릴 땐 이 환경변수를 지우는 걸 권장합니다(전원 수동 모드로 동작, 과금 없음).")
    else:
        print("ℹ️  ANTHROPIC_API_KEY가 설정되어 있지 않습니다 — '추천곡 조회'는 수동(복사-붙여넣기) 모드로 동작합니다. (과금 없음)")

    if ACCESS_CODE:
        print("🔒 접속 코드가 설정되어 있습니다. 방문자는 URL 뒤에 ?code=... 를 붙이거나, 접속 코드 입력 화면에서 코드를 입력해야 합니다.")
    else:
        print("🔓 접속 코드(ACCESS_CODE)가 설정되어 있지 않습니다 — 링크를 아는 누구나 바로 사용할 수 있습니다.")
        print("    막고 싶으면: (Windows) set ACCESS_CODE=원하는코드  (Mac/Linux) export ACCESS_CODE=원하는코드")
    print("=" * 60)

    if IS_LOCAL:
        local_url = f"http://127.0.0.1:{PORT}"
        print(f"🚀 로컬 주소: {local_url}")
        import threading
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
    else:
        print(f"🚀 포트 {PORT}에서 서버를 시작합니다 (클라우드 환경).")

    app.run(debug=False, host="0.0.0.0", port=PORT)
