#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Melon 플레이리스트 자동 진단 + AI 추천 (경량판, Chrome/Selenium 불필요)
====================================================================

이 버전은 멜론 플레이리스트 페이지가 서버에서 완성된 HTML을 내려주는 방식
(자바스크립트로 그리지 않음)이라는 걸 확인하고, requests + BeautifulSoup만으로
다시 작성한 버전입니다. Chrome이 필요 없어서 훨씬 가볍고, 무료 클라우드 호스팅
(Render 무료 플랜 등)에도 올릴 수 있습니다.

전체 흐름
---------
1. 멜론 URL 또는 plylstSeq 입력
2. requests로 메인 페이지(앞 50곡) + AJAX 엔드포인트(나머지 페이지)를 읽어
   곡 목록(제목/아티스트)과 플레이리스트 제목/소개글을 수집
   (--with-year 옵션 시 곡별 발매년도도 수집)
3. 5개 지표로 진단
   1) 아티스트 다양성  2) 중복/유사버전  3) Feat. 밀도
   4) 구간별/인접 아티스트 쏠림  5) 신선도(발매년도, 데이터 있을 때만)
   + 컨셉/제외 키워드 체크 (선택)
4. 진단 결과 + 곡 목록 + 컨셉을 Claude에 전달 → 보완용 추천곡(JSON) 생성
5. 추천곡이 실제로 멜론에 존재하는지 검색으로 재검증 (AI 환각 방지)
6. 콘솔 리포트 출력 + recommended_additions.txt 저장

설치
----
    pip install requests beautifulsoup4 anthropic

환경변수
--------
    ANTHROPIC_API_KEY   Claude API 키 (API 방식 사용 시)

사용 예시
--------
    python melon_playlist_recommender.py 508316846
    python melon_playlist_recommender.py "https://www.melon.com/mymusic/dj/mymusicdjplaylistview_inform.htm?plylstSeq=508316846" \
        --concept "여름 힙합/R&B 드라이빙" --exclude "발라드,이별" -n 10 --with-year --manual-ai
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAIN_URL_TMPL = "https://www.melon.com/mymusic/dj/mymusicdjplaylistview_inform.htm?plylstSeq={seq}"
AJAX_LISTSONG_URL = "https://www.melon.com/dj/playlist/djplaylist_listsong.htm"
SEARCH_URL = "https://www.melon.com/search/song/index.htm"

HASHTAG_PATTERN = re.compile(r"#[가-힣A-Za-z0-9_]+")
BOILERPLATE_LABELS = ["소개글", "펼치기", "접기", "더보기"]


# =========================================================
# 0. 입력 파싱
# =========================================================

def extract_plylst_seq(value: str) -> str:
    m = re.search(r"plylstSeq=(\d+)", value)
    if m:
        return m.group(1)
    if value.strip().isdigit():
        return value.strip()
    raise ValueError("멜론 플레이리스트 URL 또는 plylstSeq 숫자를 입력해주세요.")


def extract_song_id(href: str):
    if not href:
        return None
    m = re.search(r"goSongDetail\('?(\d+)'?\)", href)
    return m.group(1) if m else None


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


# =========================================================
# 1. 멜론 스크래핑 (requests + BeautifulSoup, Chrome 불필요)
# =========================================================

def parse_tracks_from_html(html):
    """플레이리스트 메인 페이지 / AJAX 다음 페이지 공통 파싱.
    곡 제목/아티스트 텍스트는 rank01/rank02 영역에 있고,
    goSongDetail 링크는 텍스트 없는 '곡정보' 아이콘 버튼이라 songId 추출용으로만 씀."""
    soup = BeautifulSoup(html, "html.parser")
    tracks = []
    for row in soup.select("table tbody tr"):
        title_a = row.select_one(".ellipsis.rank01 a")
        artist_as = row.select(".ellipsis.rank02 a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True).replace("\xa0", " ")
        artists = []
        for a in artist_as:
            t = a.get_text(strip=True).replace("\xa0", " ")
            if t:
                artists.append(t)
        artist = " & ".join(dict.fromkeys(artists))  # 중복 제거, 순서 유지
        if not title or not artist:
            continue
        song_id = None
        info_a = row.select_one("a[href*='goSongDetail']")
        if info_a and info_a.get("href"):
            song_id = extract_song_id(info_a["href"])
        tracks.append({"title": title, "artist": artist, "year": None, "song_id": song_id})
    return tracks


def fetch_all_tracks(seq, session, page_size=50, max_batches=40, debug=False):
    main_url = MAIN_URL_TMPL.format(seq=seq)
    resp = session.get(main_url, timeout=15)
    resp.raise_for_status()
    html = resp.text

    if debug:
        print(f"🐛 [debug] 메인 페이지 상태코드={resp.status_code}, 길이={len(html)}자")

    tracks = parse_tracks_from_html(html)
    seen = {(t["title"], t["artist"]) for t in tracks}

    start = page_size + 1  # 1페이지(1~50)는 메인 페이지에서 이미 확보
    for i in range(max_batches):
        params = {"startIndex": start, "pageSize": page_size, "plylstSeq": seq}
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": main_url}
        r = session.get(AJAX_LISTSONG_URL, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            if debug:
                print(f"🐛 [debug] AJAX 페이지 요청 실패 (startIndex={start}, status={r.status_code}) → 종료")
            break
        batch = parse_tracks_from_html(r.text)
        new_batch = [t for t in batch if (t["title"], t["artist"]) not in seen]
        if debug:
            print(f"🐛 [debug] AJAX startIndex={start}: {len(batch)}곡 수신 ({len(new_batch)}곡 신규)")
        if not new_batch:
            break
        for t in new_batch:
            seen.add((t["title"], t["artist"]))
        tracks.extend(new_batch)
        if len(batch) < page_size:
            break
        start += page_size

    return tracks, html


def find_description_by_hashtag_anchor(soup):
    """소개글이 '#태그 #태그...'로 끝난다는 공통점을 이용해, 해시태그를
    2개 이상 포함한 텍스트 블록을 찾고 그 중 가장 많은 맥락을 담은 상위 요소를 고른다.
    <script>/<style> 안의 jQuery 선택자(#reasonMention 등)도 '#'을 포함하므로
    그런 코드 텍스트는 후보에서 제외하기 위해 미리 제거한 복사본에서 검색한다."""
    clean = BeautifulSoup(str(soup), "html.parser")
    for tag in clean(["script", "style", "noscript"]):
        tag.decompose()

    best = None
    for el in clean.find_all(string=HASHTAG_PATTERN):
        txt = el.strip()
        if len(HASHTAG_PATTERN.findall(txt)) < 2:
            continue
        node = el.parent
        candidates = [txt]
        for _ in range(6):
            if node is None:
                break
            t = node.get_text(" ", strip=True)
            if t:
                candidates.append(t)
            node = node.parent
        candidates = [c for c in candidates if len(c) < 3000]
        if not candidates:
            continue
        local_best = max(candidates, key=len)
        if best is None or len(local_best) > len(best):
            best = local_best
    return best


def _clean_boilerplate(text):
    if not text:
        return text
    t = text.strip()
    for label in BOILERPLATE_LABELS:
        if t.startswith(label):
            t = t[len(label):].strip()
        if t.endswith(label):
            t = t[: -len(label)].strip()
    return t


def extract_meta(html, debug=False):
    """플레이리스트 제목/소개글 전체 텍스트 추출."""
    soup = BeautifulSoup(html, "html.parser")

    title = None
    for sel in [".song_name", ".dj_tit", ".tit_dj", ".dj_atist_name", "h1", "h2.tit"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            title = el.get_text(strip=True).replace("\xa0", " ")
            break

    description = None
    for sel in [".dj_atist_desc", ".dj_info p", ".wrap_dj_desc", ".desc_dj", "p.desc", ".txt_desc", ".info_desc"]:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t and (description is None or len(t) > len(description)):
                description = t

    if not description or "#" not in description or len(description) < 30:
        anchor_desc = find_description_by_hashtag_anchor(soup)
        if anchor_desc and (not description or len(anchor_desc) > len(description)):
            description = anchor_desc

    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()
    if not description:
        og = soup.find("meta", property="og:description")
        if og and og.get("content"):
            description = og["content"].strip()

    if description:
        description = _clean_boilerplate(description.replace("\xa0", " "))

    if debug:
        print(f"🐛 [debug] 추출된 title: {title!r}")
        print(f"🐛 [debug] 추출된 description 길이: {len(description) if description else 0}")
        if description:
            print(f"🐛 [debug] 추출된 description: {description!r}")

    return {"title": title, "description": description}


def fetch_year_for_song(song_id: str, session):
    """곡 상세페이지에서 발매년도를 가져온다 (--with-year 옵션일 때만 호출)."""
    url = f"https://www.melon.com/song/detail.htm?songId={song_id}"
    try:
        resp = session.get(url, timeout=8)
        resp.raise_for_status()
        m = re.search(r"발매일\s*</dt>\s*<dd>\s*(\d{4})", resp.text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def scrape_playlist(seq: str, with_year=False, debug=False, extract_meta_info=False):
    """플레이리스트의 곡 목록을 가져온다.
    extract_meta_info=True면 (tracks, meta) 튜플을, 아니면 tracks만 반환한다."""
    session = _session()
    try:
        tracks, html = fetch_all_tracks(seq, session, debug=debug)
    except requests.RequestException as e:
        if debug:
            print(f"🐛 [debug] 요청 실패: {e}")
        return ([], {"title": None, "description": None}) if extract_meta_info else []

    if with_year and tracks:
        print(f"📅 발매년도 조회 중... ({len(tracks)}곡, 시간이 걸릴 수 있습니다)")
        for t in tracks:
            if t["song_id"]:
                t["year"] = fetch_year_for_song(t["song_id"], session)
                time.sleep(0.1)

    if extract_meta_info:
        meta = extract_meta(html, debug=debug)
        return tracks, meta
    return tracks


# =========================================================
# 2. 진단 로직 (playlist_diagnostics.html 최신판과 동일 기준)
# =========================================================

def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"\[.*?\]", "", t)
    t = re.sub(r"feat\..*", "", t)
    t = re.sub(r"[^a-z0-9가-힣]", "", t)
    return t.strip()


def norm_artist(a: str) -> str:
    a = a.lower()
    a = re.sub(r"[^a-z0-9가-힣]", "", a)
    return a


def score_artist_diversity(tracks):
    n = len(tracks)
    counts = Counter(t["artist"] for t in tracks)
    entries = counts.most_common()
    top5_share = sum(c for _, c in entries[:5]) / n * 100

    if top5_share >= 40:
        score = 40
    elif top5_share >= 25:
        score = 70
    else:
        score = 90

    over_rep = [(name, c) for name, c in entries if c / n >= 0.05]
    return {
        "score": score,
        "artist_count": len(entries),
        "track_count": n,
        "top5_share": round(top5_share, 1),
        "over_represented": over_rep,
    }


def score_duplicate_versions(tracks):
    groups = defaultdict(list)
    for t in tracks:
        key = (norm_title(t["title"]), t["artist"])
        groups[key].append(t["title"])
    dups = [(k[1], v) for k, v in groups.items() if len(v) > 1]
    score = 70 if dups else 95
    return {"score": score, "duplicates": dups}


def score_feat_density(tracks):
    n = len(tracks)
    feat_count = 0
    feat_names = Counter()
    for t in tracks:
        m = re.search(r"feat\.?\s*(.*?)(?:\)|\]|$)", t["title"], re.I)
        if m:
            feat_count += 1
            for p in re.split(r",|&|/", m.group(1)):
                name = re.sub(r"\(.*?\)", "", p)
                name = re.sub(r"prod\..*", "", name, flags=re.I).strip()
                if name and len(name) <= 20:
                    feat_names[name] += 1

    ratio = feat_count / n * 100
    if ratio >= 70:
        score = 55
    elif ratio >= 50:
        score = 75
    else:
        score = 90

    repeated = [(k, v) for k, v in feat_names.most_common() if v >= 2][:6]
    return {"score": score, "feat_count": feat_count, "ratio": round(ratio, 1), "repeated_feats": repeated}


def score_section_and_adjacent(tracks):
    n = len(tracks)
    section_issues = []
    section_size = -(-n // 3)  # ceil
    section_names = ["초반부", "중반부", "후반부"]
    for s in range(3):
        start = s * section_size
        end = min(start + section_size, n)
        chunk = tracks[start:end]
        if not chunk:
            continue
        counts = Counter(t["artist"] for t in chunk)
        name, c = counts.most_common(1)[0]
        if c >= 3:
            section_issues.append(
                {"section": section_names[s], "artist": name, "count": c, "range": (start + 1, end)}
            )

    window = 4
    adjacent_flags = []
    for i in range(n):
        for j in range(i + 1, min(i + window, n)):
            if tracks[i]["artist"] == tracks[j]["artist"]:
                adjacent_flags.append({"pos1": i + 1, "pos2": j + 1, "artist": tracks[i]["artist"]})

    issues = len(section_issues) + len(adjacent_flags)
    if issues == 0:
        score = 90
    elif issues <= 3:
        score = 70
    else:
        score = 45

    return {
        "score": score,
        "section_issues": section_issues,
        "adjacent_flags": adjacent_flags,
    }


def score_freshness(tracks):
    with_year = [t for t in tracks if t.get("year")]
    if len(with_year) < len(tracks) * 0.5:
        return None

    now = datetime.now().year
    recent = [t for t in with_year if now - t["year"] <= 2]
    recent_share = len(recent) / len(with_year) * 100
    years = [t["year"] for t in with_year]

    if recent_share >= 30:
        score = 90
    elif recent_share >= 15:
        score = 70
    else:
        score = 45

    return {
        "score": score,
        "with_year_count": len(with_year),
        "track_count": len(tracks),
        "year_range": (min(years), max(years)),
        "recent_share": round(recent_share, 1),
    }


def concept_check(tracks, exclude_keywords):
    if not exclude_keywords:
        return []
    flagged = []
    for t in tracks:
        for kw in exclude_keywords:
            if kw and (kw in t["title"] or kw in t["artist"]):
                flagged.append({"title": t["title"], "artist": t["artist"], "keyword": kw})
    return flagged


def diagnose(tracks, concept=None, exclude_keywords=None):
    exclude_keywords = exclude_keywords or []
    ad = score_artist_diversity(tracks)
    dv = score_duplicate_versions(tracks)
    fd = score_feat_density(tracks)
    sa = score_section_and_adjacent(tracks)
    fr = score_freshness(tracks)
    cc = concept_check(tracks, exclude_keywords)

    scores = [ad["score"], dv["score"], fd["score"], sa["score"]]
    if fr is not None:
        scores.append(fr["score"])
    overall = round(sum(scores) / len(scores))

    return {
        "track_count": len(tracks),
        "overall": overall,
        "concept": concept,
        "artist_diversity": ad,
        "duplicate_versions": dv,
        "feat_density": fd,
        "section_adjacent": sa,
        "freshness": fr,
        "concept_flagged": cc,
    }


# =========================================================
# 3. Claude에게 추천곡 요청
# =========================================================

def build_prompt(tracks, diag, concept, exclude_keywords, n_recommend):
    track_lines = "\n".join(
        f"- {t['artist']} - {t['title']}" + (f" ({t['year']})" if t.get("year") else "")
        for t in tracks
    )
    ad, dv, fd, sa, fr = (
        diag["artist_diversity"],
        diag["duplicate_versions"],
        diag["feat_density"],
        diag["section_adjacent"],
        diag["freshness"],
    )

    over_rep = ", ".join(f"{n}({c}곡)" for n, c in ad["over_represented"]) or "없음"
    dup_lines = "; ".join(f"{a}: {'/'.join(ts)}" for a, ts in dv["duplicates"]) or "없음"
    repeated_feats = ", ".join(f"{n}({c}회)" for n, c in fd["repeated_feats"]) or "없음"
    section_lines = (
        "; ".join(f"{i['section']}({i['range'][0]}~{i['range'][1]}번)에 {i['artist']} {i['count']}곡" for i in sa["section_issues"])
        or "없음"
    )
    freshness_line = (
        f"최근 2년 이내 발매 비중 {fr['recent_share']}% (연도범위 {fr['year_range'][0]}~{fr['year_range'][1]})"
        if fr else "발매년도 데이터 부족으로 측정 안됨"
    )
    concept_line = concept or "(지정 안됨 — 아래 곡 목록의 아티스트들이 실제로 어떤 장르/사운드로 알려져 있는지 스스로 파악할 것)"
    exclude_line = ", ".join(exclude_keywords) if exclude_keywords else "없음"

    return f"""당신은 한국 스트리밍 서비스 플레이리스트 큐레이터입니다.
아래는 현재 플레이리스트의 곡 목록과 자동 진단 결과입니다.

[플레이리스트 컨셉/장르] {concept_line}
[피해야 할 키워드] {exclude_line}

[현재 곡 목록 ({len(tracks)}곡)]
{track_lines}

[진단 결과 — 메타데이터(아티스트 쏠림/중복/feat/신선도) 기준일 뿐, 장르 판단과는 무관함]
- 아티스트 다양성: {ad['score']}/100 (상위5명 점유율 {ad['top5_share']}%, 과다 노출: {over_rep})
- 중복/유사버전: {dv['score']}/100 (중복: {dup_lines})
- Feat. 밀도: {fd['score']}/100 (비율 {fd['ratio']}%, 반복 피처링: {repeated_feats})
- 구간별/인접 아티스트 쏠림: {sa['score']}/100 (구간쏠림: {section_lines}, 인접반복 {len(sa['adjacent_flags'])}건)
- 신선도: {freshness_line}
- 종합 점수: {diag['overall']}/100

[가장 중요한 원칙 — 세부 사운드 프로파일 일치가 0순위입니다]
- "록"처럼 큰 장르 카테고리만 보지 마세요. 같은 록이라도 펑크록/하드록/모던록/얼터너티브록/시티팝 성향 밴드/
  인디밴드씬/아이돌 밴드 콘셉트 등 결이 완전히 다릅니다. 아래 곡 목록을 보고 이 플레이리스트만의
  구체적인 사운드 프로파일을 먼저 파악하세요. 예를 들어 다음 요소들을 실제로 알고 있는 만큼 구체적으로 짚어내세요:
    · 세부 장르/씬 (예: 모던록 vs 하드록 vs 펑크록 vs 이모/포스트록 vs 국내 인디밴드씬 등)
    · 보컬 스타일/창법 (샤우팅 위주인지, 감성 보컬인지, 랩이 섞이는지 등)
    · 템포/에너지 (미드템포 감성 록인지, 고에너지 업템포인지)
    · 국내/해외, 시대감 (2010년대 국내 밴드씬인지, 8090 레트로 록인지, 최신 해외 록인지 등)
    · 밴드 편성 여부 (실제 밴드 사운드인지, 밴드 콘셉트의 솔로/아이돌 곡인지)
  곡 제목이나 발매시기만으로 대충 짐작하지 말고, 실제로 알고 있는 해당 아티스트/곡의 사운드 특성을 근거로
  판단하세요.
- 추천곡은 이 세부 사운드 프로파일과 실질적으로 같은 결이어야 합니다. "장르 대분류가 록이다" 정도로는
  부족합니다 — 같은 록이라도 결이 다른 타 밴드/타 씬의 곡은 추천하지 마세요.
- 아래의 "아티스트 다양성 보완", "신선도 보완" 같은 메타데이터 지표는 사운드 프로파일이 맞는 곡들 중에서만
  참고할 보조 기준입니다. 사운드가 맞지 않으면 다양성이 낮아지거나 최신곡이 아니더라도 절대 추천하지 마세요.
  "새로운 아티스트라서", "최근 발매라서", "같은 록 장르라서"라는 이유만으로 결이 다른 곡을 추천하는 것은 금지입니다.
- 확신이 서지 않는 사운드의 곡은 아예 추천 목록에서 빼세요. 애매한 곡을 넣는 것보다는 추천 개수가
  {n_recommend}개보다 적어지는 편이 낫습니다.

[그 다음 우선순위 — 세부 사운드 프로파일이 확실히 맞는 곡들 안에서]
1. 위 진단에서 약점으로 지적된 부분(특정 아티스트 쏠림, 중복 버전, feat. 과다, 구간 쏠림, 신선도 부족 등)을 보완
2. "피해야 할 키워드"에 해당하는 분위기의 곡은 제외
3. 과다 노출된 아티스트는 제외하고, 새로운 아티스트 위주로 다양성을 높일 것 (단, 사운드가 맞을 때만)
4. 반드시 실제로 존재하는 곡만 제안할 것 (지어내지 말 것)
5. 신선도가 낮다고 진단되면 최근 1~2년 내 발매곡을 우선적으로 섞되, 사운드가 맞는 곡 중에서만

[결과물의 깊이 — 단순 "장르가 맞아서" 수준이 아니라, 음악 저널리스트/디깅 큐레이터가 근거를 대는 수준이어야 합니다]
각 추천곡마다 아래 항목을 모두 채우세요. 하나라도 피상적이면(예: "이 아티스트는 유명해서") 그 추천은 빼고
더 확실히 아는 곡으로 채우세요.

- "tier": "essential" 또는 "deepcut" 둘 중 하나.
    · "essential" = 이 컨셉/장르를 대표하는 필수급 곡. 모르면 플레이리스트의 설득력이 떨어지는 수준.
      대중적 인지도, 음악사적 위치(해당 장르/씬의 기준점이 되는 곡인지), 혹은 큰 파급력(드라마/영화 OST,
      음원차트 역주행, 시대를 대표하는 히트곡 등)이 근거가 되어야 합니다.
    · "deepcut" = 핵심 팬/마니아까지 고려했을 때 추가하면 플레이리스트의 깊이가 좋아지는 곡.
      대중성은 essential보다 낮아도 되지만, 그 씬/장르 안에서는 분명한 평가나 인지도가 있어야 합니다.
    - essential이 n_recommend의 과반 이상이 되게 하려 애쓰지 말고, 실제로 아는 만큼만 essential로 분류하세요.
      확신이 없으면 deepcut으로 낮추거나 추천에서 빼세요.
- "star": 1~5 사이 정수. 이 플레이리스트에 넣을 추천 강도(5 = 반드시 넣어야 함, 1 = 있으면 좋음 정도).
- "year": 발매년도 (아는 경우만, 모르면 null).
- "album": 수록 앨범명 (아는 경우만, 모르면 null).
- "evidence": 왜 이 곡이 essential/deepcut인지에 대한 구체적 근거. 다음 중 실제로 아는 것만 섞어서
  2~3문장으로: 발매 당시/현재의 대중적 반응, 음원차트/스트리밍 플랫폼 선정 자료(멜론/벅스/지니/웨이브 등
  실제로 존재한다고 아는 것만), 영화/드라마/광고 OST 여부, 평론가/매체의 평가, 후속 세대 아티스트에게
  미친 영향, 라이브/공연에서의 위치(앤콜곡, 시그니처 곡 등). 모르는 사실을 지어내지 말고, 아는 근거가
  부족하면 "확신도가 낮다"는 취지로 솔직히 쓰고 해당 곡은 deepcut 이하로 낮추거나 제외하세요.
- "sound_match": 위에서 파악한 세부 사운드 프로파일과 정확히 어떻게 일치하는지 (보컬스타일/템포/씬/편성 등
  구체적 근거, "록이라서"처럼 대분류만 말하는 것은 금지).
- "comparison": 현재 플레이리스트에 이미 들어있는 특정 곡과 비교해서 한 문장으로 위치를 짚으세요.
  예: "현재 목록엔 {{비슷한 결의 기존곡}}만 있는데, 대중성/역사성 면에서는 이 곡이 더 핵심적이라 우선순위가
  더 높음" 또는 "{{기존곡}}과 결이 비슷하지만 더 마니아 지향적인 확장 곡" 또는 "기존 목록에 겹치는 곡이
  없어 새로운 결을 더해줌". 비교할 만한 기존곡이 없으면 그 사실을 그대로 쓰세요.
- "replace_candidate": 만약 이 추천곡이 기존 목록의 특정 곡보다 명백히 더 대표성/완성도가 높아서 "교체"를
  고려할 만하면 그 기존곡명을 적고 이유를 덧붙이세요 (예: "교체 후보: {{기존곡}} — 같은 자리의 곡 중 대중적
  대표성이 이 곡이 더 높음"). 교체를 고려할 이유가 없으면 null.

[분류 사용법]
- "우선 추가 추천 (거의 필수급)" = tier가 "essential"인 곡들
- "마니아 쪽까지 고려하면 추가할 곡" = tier가 "deepcut"인 곡들
이 두 그룹으로 화면에 나눠 보여줄 것이므로, 각 추천곡의 tier 분류가 실제 설득력과 일치하도록 신중하게 매기세요.

다음 JSON 형식으로만 답하세요 (다른 텍스트나 코드블록 표시 없이 JSON만):
{{
  "inferred_genre": "곡 목록을 분석해서 파악한 세부 사운드 프로파일 (장르/씬/보컬스타일/템포/국내외 등을 구체적으로, 2~3문장)",
  "recommendations": [
    {{
      "artist": "아티스트명",
      "title": "곡 제목",
      "tier": "essential",
      "star": 5,
      "year": 2016,
      "album": "앨범명 또는 null",
      "sound_match": "세부 사운드 프로파일과의 일치 근거",
      "evidence": "대중성/역사성/파급력 등 구체적 근거",
      "comparison": "기존 목록 곡과 비교한 한 문장",
      "replace_candidate": "교체를 고려할 기존곡명과 이유, 또는 null"
    }}
  ]
}}
"""


def _parse_ai_json(text):
    """AI 응답 텍스트에서 JSON 부분만 추출해서 파싱한다."""
    text = text.strip()
    text = re.sub(r"^```json|^```|```$", "", text, flags=re.M).strip()
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)
    return json.loads(text)


def get_recommendations(tracks, diag, concept, exclude_keywords, n_recommend=8, model="claude-sonnet-5"):
    """API 방식: ANTHROPIC_API_KEY 필요 (종량제 과금)."""
    from anthropic import Anthropic

    client = Anthropic()
    prompt = build_prompt(tracks, diag, concept, exclude_keywords, n_recommend)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    try:
        data = _parse_ai_json(text)
    except json.JSONDecodeError:
        print("⚠️ AI 응답을 JSON으로 파싱하지 못했습니다. 원본 출력:")
        print(text)
        return []
    if data.get("inferred_genre"):
        print(f"🎧 AI가 파악한 장르/사운드: {data['inferred_genre']}")
    return data.get("recommendations", [])


def manual_ai_recommendations(
    tracks, diag, concept, exclude_keywords, n_recommend=8,
    prompt_path="ai_prompt.txt", response_path="ai_response.txt",
):
    """API 키 없이, claude.ai(웹/앱) 채팅에 직접 복사-붙여넣기로 진행하는 방식 (과금 없음)."""
    prompt = build_prompt(tracks, diag, concept, exclude_keywords, n_recommend)
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    print(f"\n📝 프롬프트를 '{prompt_path}' 에 저장했습니다.")
    print("   1) 이 파일 내용을 전부 복사해서 https://claude.ai 채팅창에 붙여넣으세요.")
    print("   2) 받은 답변(JSON) 전체를 복사해서 새로 열리는 메모장/파일에 붙여넣고,")
    print(f"      '{response_path}' 라는 이름으로 이 스크립트와 같은 폴더에 저장하세요.")
    input("   3) 저장했으면 여기로 돌아와서 Enter를 눌러주세요...")

    while True:
        try:
            with open(response_path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            input(f"   '{response_path}' 파일을 찾을 수 없습니다. 저장 후 다시 Enter를 눌러주세요...")
            continue

        try:
            data = _parse_ai_json(text)
            if data.get("inferred_genre"):
                print(f"🎧 AI가 파악한 장르/사운드: {data['inferred_genre']}")
            return data.get("recommendations", [])
        except json.JSONDecodeError:
            print("⚠️ JSON 파싱에 실패했습니다. 답변에 JSON 외 다른 텍스트가 섞여있지 않은지 확인해주세요.")
            input(f"   '{response_path}' 내용을 수정한 뒤 Enter를 눌러 다시 시도하세요...")


# =========================================================
# 4. 추천곡 실존 여부 검증 (AI 환각 방지, Chrome 불필요)
# =========================================================

def verify_on_melon(recommendations, debug=False):
    session = _session()
    for rec in recommendations:
        query = f"{rec['artist']} {rec['title']}"
        rec["search_url"] = f"{SEARCH_URL}?q={requests.utils.quote(query)}"
        rec["verified"] = False
        try:
            r = session.get(SEARCH_URL, params={"q": query}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            if debug:
                print(f"🐛 [debug] 검색 요청 실패: {query} ({e})")
            continue

        candidates = parse_tracks_from_html(r.text)
        if debug:
            print(f"🐛 [debug] '{query}' 검색결과 {len(candidates)}개")
        for cand in candidates[:5]:
            same_title = norm_title(cand["title"]) == norm_title(rec["title"])
            na_rec, na_found = norm_artist(rec["artist"]), norm_artist(cand["artist"])
            same_artist = bool(na_rec) and bool(na_found) and (na_rec in na_found or na_found in na_rec)
            if debug:
                print(f"🐛 [debug]   후보: {cand['artist']} - {cand['title']} "
                      f"(title_match={same_title}, artist_match={same_artist})")
            if same_title and same_artist:
                rec["verified"] = True
                rec["melon_title"] = cand["title"]
                rec["melon_artist"] = cand["artist"]
                break
    return recommendations


# =========================================================
# 5. 출력 (CLI용)
# =========================================================

def print_diagnosis(diag):
    print("\n===== 진단 결과 =====")
    print(f"종합 점수: {diag['overall']}/100  (총 {diag['track_count']}곡)")
    print(f"1. 아티스트 다양성        : {diag['artist_diversity']['score']}/100")
    print(f"2. 중복/유사 버전         : {diag['duplicate_versions']['score']}/100")
    print(f"3. Feat. 밀도             : {diag['feat_density']['score']}/100")
    print(f"4. 구간별/인접 아티스트 쏠림: {diag['section_adjacent']['score']}/100")
    if diag["freshness"]:
        print(f"5. 신선도                 : {diag['freshness']['score']}/100")
    else:
        print("5. 신선도                 : 발매년도 데이터 부족 (--with-year 옵션으로 수집 가능)")
    if diag["concept_flagged"]:
        print(f"⚠️ 컨셉 키워드 위반 곡 {len(diag['concept_flagged'])}건")


def print_recommendations(recommendations):
    if not recommendations:
        print("\n(추천곡 없음)")
        return
    print("\n===== AI 추천곡 =====")
    for i, r in enumerate(recommendations, 1):
        mark = "✅" if r.get("verified") else "⚠️ 실존 확인 안됨"
        print(f"{i}. [{mark}] {r['artist']} - {r['title']}")
        print(f"   이유: {r['reason']}")


def save_paste_ready(recommendations, path="recommended_additions.txt", only_verified=False):
    rows = [r for r in recommendations if (r.get("verified") if only_verified else True)]
    lines = [f"{r['title']}\t{r['artist']}" for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n📋 '{path}' 저장됨 ({len(lines)}곡)")
    print("   → playlist_diagnostics.html의 곡 목록 맨 아래에 붙여넣고 다시 분석해보세요.")
    return path


def save_readable_report(recommendations, path="recommendations_report.txt"):
    lines = ["===== AI 추천곡 =====\n"]
    for i, r in enumerate(recommendations, 1):
        mark = "확인됨" if r.get("verified") else "실존 확인 안됨 (직접 확인 필요)"
        lines.append(f"{i}. {r['artist']} - {r['title']}  [{mark}]")
        lines.append(f"   이유: {r['reason']}")
        if r.get("search_url"):
            lines.append(f"   멜론 검색: {r['search_url']}")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def open_in_default_app(path):
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
        return True
    except Exception as e:
        print(f"⚠️ 파일을 자동으로 여는 데 실패했습니다 ({e}). 직접 '{path}' 파일을 열어주세요.")
        return False


def open_search_tabs(recommendations, only_verified=False, delay=0.4):
    rows = [r for r in recommendations if (r.get("verified") if only_verified else True)]
    if not rows:
        return
    print(f"\n🌐 브라우저에서 추천곡 {len(rows)}개의 멜론 검색 결과를 새 탭으로 엽니다...")
    for r in rows:
        url = r.get("search_url") or (
            f"{SEARCH_URL}?q={requests.utils.quote(r['artist'] + ' ' + r['title'])}"
        )
        webbrowser.open_new_tab(url)
        time.sleep(delay)


def _norm_exclude(exclude):
    if not exclude:
        return []
    if isinstance(exclude, str):
        return [k.strip() for k in exclude.split(",") if k.strip()]
    return [k.strip() for k in exclude if k and k.strip()]


# =========================================================
# main (CLI)
# =========================================================

def main():
    parser = argparse.ArgumentParser(description="Melon 플레이리스트 진단 + AI 추천 (Chrome 불필요)")
    parser.add_argument("playlist", help="멜론 플레이리스트 URL 또는 plylstSeq")
    parser.add_argument("--concept", default=None, help="플레이리스트 컨셉/무드")
    parser.add_argument("--exclude", default=None, help="피하고 싶은 키워드 (쉼표로 구분)")
    parser.add_argument("-n", "--num", type=int, default=8, help="추천곡 개수 (기본 8)")
    parser.add_argument("--with-year", action="store_true", help="발매년도까지 조회 (느려짐, 신선도 분석용)")
    parser.add_argument("--no-verify", action="store_true", help="추천곡 실존 검증 생략 (속도 향상)")
    parser.add_argument("--debug", action="store_true", help="실패 원인을 콘솔에 자세히 출력")
    parser.add_argument("--model", default="claude-sonnet-5", help="사용할 Claude 모델 (API 방식일 때만)")
    parser.add_argument(
        "--manual-ai", action="store_true",
        help="API 대신 claude.ai에 직접 복사-붙여넣기로 추천곡을 받음 (API 과금 없음)",
    )
    parser.add_argument("--no-open-report", action="store_true", help="추천곡 리포트를 메모장으로 자동으로 여는 것 생략")
    parser.add_argument("--no-open-tabs", action="store_true", help="추천곡별 멜론 검색결과를 새 탭으로 여는 것 생략")
    args = parser.parse_args()

    exclude_keywords = _norm_exclude(args.exclude)

    seq = extract_plylst_seq(args.playlist)
    print(f"🔍 plylstSeq={seq} 조회 중...")
    tracks, meta = scrape_playlist(seq, with_year=args.with_year, debug=args.debug, extract_meta_info=True)
    if not tracks:
        print("❌ 곡 목록을 가져오지 못했습니다. --debug 옵션을 붙여서 다시 실행해보세요.")
        sys.exit(1)
    print(f"✅ {len(tracks)}곡 수집 완료")
    if meta.get("title"):
        print(f"📀 제목: {meta['title']}")

    concept = args.concept or meta.get("description")

    diag = diagnose(tracks, concept=concept, exclude_keywords=exclude_keywords)
    print_diagnosis(diag)

    if args.manual_ai:
        recs = manual_ai_recommendations(tracks, diag, concept, exclude_keywords, n_recommend=args.num)
    else:
        print("\n🤖 Claude API로 추천곡 요청 중...")
        recs = get_recommendations(
            tracks, diag, concept, exclude_keywords, n_recommend=args.num, model=args.model
        )

    if recs and not args.no_verify:
        print("🔎 멜론에서 추천곡 실존 여부 확인 중...")
        recs = verify_on_melon(recs, debug=args.debug)

    print_recommendations(recs)
    save_paste_ready(recs, only_verified=False)

    if recs:
        report_path = save_readable_report(recs)
        print(f"📄 '{report_path}' 저장됨")
        if not args.no_open_report:
            open_in_default_app(os.path.abspath(report_path))
        if not args.no_open_tabs:
            open_search_tabs(recs, only_verified=False)


if __name__ == "__main__":
    main()
