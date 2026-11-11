import os
import re
import json
import requests
import cloudscraper
import feedparser
from datetime import datetime, timezone
import pytz
import markdown
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is missing in GitHub Secrets.")

client = genai.Client(
    api_key=api_key,
    http_options=types.HttpOptions(timeout=60000)
)

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

ARSENAL_ESPN_ID = "359"
ARSENAL_FOTMOB_ID = "9825"

# コピー時にドメイン名がMarkdownリンク化されるのを100%防ぐURL生成関数
def build_url(*parts):
    raw = "".join(parts)
    # 万が一 [xxx](yyy) のようなリンク記法が紛れ込んでも自動除去
    raw = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', raw)
    return raw.replace("[", "").replace("]", "")

# ドメイン部品（分割定義で自動リンク化を完全防止）
P_HTTPS = "https" + "://"
D_GUARDIAN = "www.theguardian" + ".com"
D_BBC = "feeds.bbci" + ".co.uk"
D_ARSEBLOG = "arseblog" + ".news"
D_FLONDON = "www.football" + ".london"
D_ESPN = "site.api.espn" + ".com"
D_FOTMOB = "www.fotmob" + ".com"
D_FPL = "fantasy.premierleague" + ".com"

# --------------------------------------------------
# 1. Geminiモデル自動選択 & 大量ニュース翻訳
# --------------------------------------------------
def generate_with_best_model(prompt):
    candidates = ["gemini-3.8-flash", "gemini-3.0-flash", "gemini-3-flash"]
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            if "flash" in name and not any(x in name for x in ["1.5", "2.0", "2.5", "vision", "audio", "tts", "8b"]):
                if name not in candidates:
                    candidates.insert(0, name)
            elif "flash" in name and not any(x in name for x in ["1.5", "vision", "audio", "tts"]):
                if name not in candidates:
                    candidates.append(name)
    except Exception:
        pass

    last_err = ""
    for model_name in candidates:
        try:
            res = client.models.generate_content(model=model_name, contents=prompt)
            if res and res.text:
                return res.text.replace("```html", "").replace("```", "").strip()
        except Exception as e:
            last_err = str(e)
            continue
    return f"<p>ニュース要約エラー: {last_err}</p>"

def get_all_uk_news():
    feeds = {
        "Arsenal (Arseblog)": (build_url(P_HTTPS, D_ARSEBLOG, "/feed/"), 7),
        "Arsenal (Football.London)": (build_url(P_HTTPS, D_FLONDON, "/arsenal-fc/?service=rss"), 6),
        "Arsenal (Guardian)": (build_url(P_HTTPS, D_GUARDIAN, "/football/arsenal/rss"), 6),
        "Arsenal (BBC)": (build_url(P_HTTPS, D_BBC, "/sport/football/teams/arsenal/rss.xml"), 5),
        "Premier League (Guardian)": (build_url(P_HTTPS, D_GUARDIAN, "/football/premierleague/rss"), 8),
        "Premier League (BBC)": (build_url(P_HTTPS, D_BBC, "/sport/football/premier-league/rss.xml"), 6),
        "Transfer Gossip (BBC)": (build_url(P_HTTPS, D_BBC, "/sport/football/gossip/rss.xml"), 1)
    }
    
    raw_data = []
    seen_titles = set()
    for name, (url, limit) in feeds.items():
        try:
            resp = scraper.get(url, timeout=10)
            f = feedparser.parse(resp.content)
            count = 0
            for e in f.entries:
                if count >= limit or e.title in seen_titles:
                    continue
                seen_titles.add(e.title)
                clean_summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()
                raw_data.append(f"[{name}]\nTitle: {e.title}\nSummary: {clean_summary[:380]}\nLink: {e.link}\n")
                count += 1
        except Exception as e:
            print(f"RSSスキップ ({name}): {e}", flush=True)

    if not raw_data:
        return "<p>英語ニュースフィードを取得できませんでした。</p>"

    prompt = f"""
あなたはアーセナルとプレミアリーグ専門の翻訳編集者です。
以下の現地メディアの最新記事を日本語に翻訳・詳細要約し、指定のHTMLタグ構造のみで出力してください。
※コードブロック（```html等）は絶対につけないでください。できるだけ多くの記事を掲載してください。

<h2>🔴 アーセナル最新ニュース（現地番記者・専門メディア）</h2>
アーセナルに関する記事をすべて抽出し（12〜18件程度）、1記事ずつ以下の折りたたみ形式で出力：
<details>
  <summary>🔴 [メディア名] [魅力的で具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li>[記事の具体的な内容・発言・背景1（詳しく）]</li>
      <li>[翻訳ポイント2（詳しく）]</li>
      <li>[翻訳ポイント3（戦術・怪我人・今後の見通しなど）]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>⚔️ ビッグ6＆プレミアリーグ動向</h2>
マンチェスターC、リヴァプール、チェルシー、マンチェスターU、トッテナム等の主要記事（8〜12件程度）を <details><summary>⚔️ 【クラブ名】[日本語タイトル]</summary>... の形式で出力。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip / 現地紙まとめ)</h2>
アーセナルを最優先に、プレミアリーグの移籍の噂を1件ずつ <details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>... の形式で詳しく出力。

【入力データ】:
{chr(10).join(raw_data)}
"""
    return generate_with_best_model(prompt)

# --------------------------------------------------
# 2. 次回日程(JST) ＆ 前回試合スタッツ（地上戦デュエル・個人採点・出場時間）
# --------------------------------------------------
def get_match_and_stats():
    jst = pytz.timezone("Asia/Tokyo")
    now_utc = datetime.now(timezone.utc)
    next_html = "<div class='next-box'>次回試合予定データ取得中</div>"
    stats_md = "<p>スタッツデータなし</p>"

    upcoming_list = []
    finished_list = []
    leagues = [
        ("eng.1", "Premier League"),
        ("uefa.champions", "Champions League"),
        ("eng.fa", "FA Cup"),
        ("eng.league_cup", "Carabao Cup")
    ]

    # --- A. ESPN APIから過去試合と未来試合を取得 ---
    for lg_code, lg_name in leagues:
        try:
            url_past = build_url(P_HTTPS, D_ESPN, f"/apis/site/v2/sports/soccer/{lg_code}/teams/{ARSENAL_ESPN_ID}/schedule")
            res_past = requests.get(url_past, headers=HEADERS, timeout=8).json()
            for ev in res_past.get("events", []):
                ev_dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                state = ev.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state", "")
                ev["_lg_code"] = lg_code
                ev["_lg_name"] = lg_name
                ev["_dt"] = ev_dt
                if state == "post" or ev_dt < now_utc:
                    comps = ev.get("competitions", [{}])[0].get("competitors", [])
                    if comps and "score" in comps[0]:
                        finished_list.append(ev)
                elif state == "pre" or ev_dt >= now_utc:
                    upcoming_list.append(ev)
        except Exception as e:
            print(f"ESPN past err ({lg_code}): {e}", flush=True)

        try:
            url_fut = build_url(P_HTTPS, D_ESPN, f"/apis/site/v2/sports/soccer/{lg_code}/teams/{ARSENAL_ESPN_ID}/schedule?fixture=true")
            res_fut = requests.get(url_fut, headers=HEADERS, timeout=8).json()
            for ev in res_fut.get("events", []):
                ev_dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                if ev_dt >= now_utc:
                    ev["_lg_code"] = lg_code
                    ev["_lg_name"] = lg_name
                    ev["_dt"] = ev_dt
                    upcoming_list.append(ev)
        except Exception as e:
            print(f"ESPN fut err ({lg_code}): {e}", flush=True)

    # もしESPNのリストに次回試合がなければFPL公式APIから補完
    if not upcoming_list:
        try:
            boot_url = build_url(P_HTTPS, D_FPL, "/api/bootstrap-static/")
            boot = requests.get(boot_url, headers=HEADERS, timeout=8).json()
            t_map = {t["id"]: t["name"] for t in boot.get("teams", [])}
            ars_id = next((t["id"] for t in boot.get("teams", []) if t["name"] == "Arsenal"), 1)
            fix_url = build_url(P_HTTPS, D_FPL, "/api/fixtures/")
            fixes = requests.get(fix_url, headers=HEADERS, timeout=8).json()
            fpl_up = [f for f in fixes if (f.get("team_h") == ars_id or f.get("team_a") == ars_id) and not f.get("finished") and f.get("kickoff_time")]
            if fpl_up:
                nxt = fpl_up[0]
                utc_dt = datetime.fromisoformat(nxt["kickoff_time"].replace("Z", "+00:00"))
                jst_dt = utc_dt.astimezone(jst)
                h_name = t_map.get(nxt["team_h"], "Home")
                a_name = t_map.get(nxt["team_a"], "Away")
                next_html = (
                    f"<div class='next-box'>"
                    f"<b>⏰ 次回試合: {h_name} vs {a_name}</b><br>"
                    f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ (PL 第{nxt.get('event', '-')}節)"
                    f"</div>"
                )
        except Exception as e:
            print(f"FPL err: {e}", flush=True)
    else:
        upcoming_list.sort(key=lambda x: x["_dt"])
        nxt = upcoming_list[0]
        jst_dt = nxt["_dt"].astimezone(jst)
        teams = nxt["competitions"][0]["competitors"]
        home = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "home"), "")
        away = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "away"), "")
        next_html = (
            f"<div class='next-box'>"
            f"<b>⏰ 次回試合: {home} vs {away}</b><br>"
            f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ ({nxt['_lg_name']})"
            f"</div>"
        )

    # --- B. FotMobから超詳細スタッツ・地上戦デュエル・個人採点を取得 ---
    fotmob_lines = []
    try:
        fm_team_url = build_url(P_HTTPS, D_FOTMOB, f"/api/teams?id={ARSENAL_FOTMOB_ID}&ccode3=JPN")
        fm_res = scraper.get(fm_team_url, timeout=10)
        if fm_res.status_code == 200:
            fm_data = fm_res.json()
            all_fix = fm_data.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
            
            if "取得中" in next_html:
                fm_up = [m for m in all_fix if not m.get("status", {}).get("started")]
                if fm_up:
                    nxt_fm = fm_up[0]
                    utc_dt = datetime.fromisoformat(nxt_fm["status"]["utcTime"].replace("Z", "+00:00"))
                    jst_dt = utc_dt.astimezone(jst)
                    next_html = (
                        f"<div class='next-box'>"
                        f"<b>⏰ 次回試合: {nxt_fm['home']['name']} vs {nxt_fm['away']['name']}</b><br>"
                        f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ ({nxt_fm.get('tournament', {}).get('name', '')})"
                        f"</div>"
                    )

            fm_fin = [m for m in all_fix if m.get("status", {}).get("finished")]
            if fm_fin:
                last_match_id = fm_fin[-1]["id"]
                det_url = build_url(P_HTTPS, D_FOTMOB, f"/api/matchDetails?matchId={last_match_id}&lang=ja")
                det_res = scraper.get(det_url, timeout=10)
                if det_res.status_code == 200:
                    d_json = det_res.json()
                    h_team = d_json.get("general", {}).get("homeTeam", {}).get("name", "Home")
                    a_team = d_json.get("general", {}).get("awayTeam", {}).get("name", "Away")
                    score_s = d_json.get("header", {}).get("status", {}).get("scoreStr", "-")
                    fotmob_lines.append(f"### {h_team} {score_s} {a_team}\n")

                    shots = d_json.get("content", {}).get("shotmap", {}).get("shots", [])
                    if shots:
                        fotmob_lines.append("#### 🎯 ゴール＆シュート詳細 (xG / xGOT)")
                        fotmob_lines.append("| 分 | 選手 | 結果 | 部位 | 状況 | xG | xGOT |")
                        fotmob_lines.append("| :--- | :--- | :--- | :--- | :--- | :---: | :---: |")
                        for s in shots:
                            is_g = (s.get("eventType") == "Goal")
                            r_mark = "⚽ **GOAL**" if is_g else s.get("eventType", "")
                            xg = f"{s.get('expectedGoals', 0):.2f}" if s.get("expectedGoals") else "-"
                            xgot = f"{s.get('expectedGoalsOnTarget', 0):.2f}" if s.get("expectedGoalsOnTarget") else "-"
                            fotmob_lines.append(f"| {s.get('min')}' | **{s.get('playerName')}** | {r_mark} | {s.get('shotType')} | {s.get('situation')} | {xg} | {xgot} |")
                        fotmob_lines.append("")

                    lineup_data = d_json.get("content", {}).get("lineup", {})
                    for side in ["homeTeam", "awayTeam"]:
                        t_obj = lineup_data.get(side, {})
                        if str(t_obj.get("id")) == ARSENAL_FOTMOB_ID or "Arsenal" in t_obj.get("name", ""):
                            fotmob_lines.append("#### 👥 アーセナル選手採点・地上戦デュエル・個人スタッツ")
                            fotmob_lines.append("| 選手 | ポジ | 採点 | 時間 | G/A | シュート | パス成功 | 地上デュエル | 空中戦 |")
                            fotmob_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                            
                            all_players = t_obj.get("starters", []) + t_obj.get("subs", [])
                            for p in all_players:
                                p_name = p.get("name", "")
                                pos = p.get("positionStringShort", "-")
                                rating = p.get("performance", {}).get("rating", "-")
                                if isinstance(rating, (int, float)):
                                    rating = f"**{rating:.1f}**"
                                
                                p_stats = {}
                                for st_group in p.get("stats", []):
                                    for st_key, st_val in st_group.get("stats", {}).items():
                                        p_stats[st_key] = st_val.get("stat", {}).get("value", "-")
                                
                                mins = p_stats.get("Minutes played", "-")
                                if mins == "-" and p in t_obj.get("subs", []):
                                    continue
                                goals = p_stats.get("Goals", 0)
                                assists = p_stats.get("Assists", 0)
                                ga_str = f"{goals}G {assists}A" if (goals or assists) else "-"
                                sh = p_stats.get("Total shots", "-")
                                pa = p_stats.get("Accurate passes", "-")
                                g_duel = p_stats.get("Ground duels won", "-")
                                a_duel = p_stats.get("Aerial duels won", "-")
                                
                                fotmob_lines.append(f"| **{p_name}** | {pos} | {rating} | {mins}' | {ga_str} | {sh} | {pa} | {g_duel} | {a_duel} |")
                            fotmob_lines.append("")

                    stats_groups = d_json.get("content", {}).get("stats", {}).get("Periods", {}).get("All", {}).get("stats", [])
                    for group in stats_groups:
                        fotmob_lines.append(f"#### 📊 {group.get('title', 'スタッツ')}")
                        fotmob_lines.append(f"| 項目 | {h_team} | {a_team} |")
                        fotmob_lines.append("| :--- | :---: | :---: |")
                        for item in group.get("stats", []):
                            if item.get("type") == "title":
                                continue
                            vals = item.get("stats", ["-", "-"])
                            if len(vals) >= 2 and vals[0] is not None:
                                fotmob_lines.append(f"| {item.get('title', '')} | {vals[0]} | {vals[1]} |")
                        fotmob_lines.append("")
    except Exception as e:
        print(f"FotMob取得スキップ: {e}", flush=True)

    if fotmob_lines:
        stats_md = markdown.markdown("\n".join(fotmob_lines), extensions=['tables'])
        return next_html, stats_md

    # --- C. FotMobが弾かれた場合のESPN詳細スタッツ＆個人スタッツ ---
    if finished_list:
        try:
            finished_list.sort(key=lambda x: x["_dt"])
            last_ev = finished_list[-1]
            last_id = last_ev["id"]
            lg_code = last_ev["_lg_code"]
            lg_name = last_ev["_lg_name"]

            sum_url = build_url(P_HTTPS, D_ESPN, f"/apis/site/v2/sports/soccer/{lg_code}/summary?event={last_id}")
            sum_res = requests.get(sum_url, headers=HEADERS, timeout=10).json()
            
            comp = sum_res["header"]["competitions"][0]
            t_home = next(t for t in comp["competitors"] if t["homeAway"] == "home")
            t_away = next(t for t in comp["competitors"] if t["homeAway"] == "away")
            h_id = str(t_home["team"]["id"])
            a_id = str(t_away["team"]["id"])
            h_name = t_home["team"]["displayName"]
            a_name = t_away["team"]["displayName"]
            h_score = t_home.get("score", "0")
            a_score = t_away.get("score", "0")
            if isinstance(h_score, dict):
                h_score = h_score.get("displayValue", "0")
            if isinstance(a_score, dict):
                a_score = a_score.get("displayValue", "0")
            
            lines = [f"### {h_name} {h_score} - {a_score} {a_name} ({lg_name})\n"]

            details = comp.get("details", [])
            if details:
                lines.append("#### ⚽ ゴール＆試合イベント詳細")
                lines.append("| 時間 | チーム | 選手 | 内容 |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for d in details:
                    clock = d.get("clock", {}).get("displayValue", "")
                    t_id = str(d.get("team", {}).get("id", ""))
                    t_label = h_name if t_id == h_id else a_name
                    parts = d.get("participants", [])
                    p_name = parts[0].get("athlete", {}).get("displayName", "-") if parts else "-"
                    d_type = d.get("type", {}).get("text", "")
                    lines.append(f"| {clock} | {t_label} | **{p_name}** | {d_type} |")
                lines.append("")

            rosters = sum_res.get("rosters", [])
            for r in rosters:
                if str(r.get("team", {}).get("id", "")) == ARSENAL_ESPN_ID:
                    lines.append("#### 👥 アーセナル出場選手＆個人スタッツ")
                    lines.append("| 選手 | ポジ | 出場 | シュート(枠内) | G / A | タックル/ファウル |")
                    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
                    for ath in r.get("roster", []):
                        starter = "先発" if ath.get("starter") else ("交代出場" if ath.get("subbedIn") else "")
                        if not starter:
                            continue
                        p_name = ath.get("athlete", {}).get("displayName", "")
                        pos = ath.get("position", {}).get("abbreviation", "-")
                        st_map = {s["name"]: s["displayValue"] for s in ath.get("stats", [])}
                        sh = f"{st_map.get('totalShots', '0')} ({st_map.get('shotsOnTarget', '0')})"
                        ga = f"{st_map.get('totalGoals', '0')}G / {st_map.get('goalAssists', '0')}A"
                        tf = f"T:{st_map.get('totalTackles', '0')} / F:{st_map.get('foulsCommitted', '0')}"
                        lines.append(f"| **{p_name}** | {pos} | {starter} | {sh} | {ga} | {tf} |")
                    lines.append("")

            box_teams = sum_res.get("boxscore", {}).get("teams", [])
            if len(box_teams) >= 2:
                h_stats, a_stats = {}, {}
                for bt in box_teams:
                    bt_id = str(bt.get("team", {}).get("id", ""))
                    st_dict = {s["name"]: s["displayValue"] for s in bt.get("statistics", [])}
                    if bt_id == h_id:
                        h_stats = st_dict
                    elif bt_id == a_id:
                        a_stats = st_dict

                lines.append("#### 📊 チーム詳細スタッツ比較")
                lines.append(f"| 項目 | {h_name} | {a_name} |")
                lines.append("| :--- | :---: | :---: |")
                label_map = [
                    ("possessionPct", "ボール支配率 (%)"),
                    ("totalShots", "総シュート数"),
                    ("shotsOnTarget", "枠内シュート数"),
                    ("shotPct", "枠内シュート率 (%)"),
                    ("blockedShots", "ブロックされたシュート"),
                    ("wonCorners", "コーナーキック"),
                    ("offsides", "オフサイド"),
                    ("totalPasses", "総パス数"),
                    ("accuratePasses", "成功パス数"),
                    ("passPct", "パス成功率 (%)"),
                    ("totalCrosses", "クロス総数"),
                    ("accurateCrosses", "成功クロス数"),
                    ("totalLongBalls", "ロングボール総数"),
                    ("accurateLongBalls", "成功ロングボール数"),
                    ("totalTackles", "タックル総数"),
                    ("effectiveTackles", "タックル成功数"),
                    ("tacklePct", "タックル成功率 (%)"),
                    ("interceptions", "インターセプト"),
                    ("effectiveClearance", "クリア数"),
                    ("saves", "GKセーブ数"),
                    ("foulsCommitted", "ファウル数"),
                    ("yellowCards", "イエローカード"),
                    ("redCards", "レッドカード")
                ]
                for key, jp in label_map:
                    if key in h_stats or key in a_stats:
                        lines.append(f"| {jp} | {h_stats.get(key, '-')} | {a_stats.get(key, '-')} |")
                lines.append("")

            stats_md = markdown.markdown("\n".join(lines), extensions=['tables'])
        except Exception as e:
            stats_md = f"<p>スタッツ解析エラー: {e}</p>"

    return next_html, stats_md

# --------------------------------------------------
# 3. プレミアリーグ最新順位表の取得
# --------------------------------------------------
def get_pl_standings():
    try:
        url = build_url(P_HTTPS, D_ESPN, "/apis/v2/sports/soccer/eng.1/standings")
        res = requests.get(url, headers=HEADERS, timeout=10).json()
        entries = res.get("children", [{}])[0].get("standings", {}).get("entries", [])
        if not entries:
            return "<p>順位表データなし</p>"

        lines = [
            "### 🏆 プレミアリーグ 最新順位表\n",
            "| 順位 | クラブ | 試 | 勝 | 分 | 負 | 得失 | 勝点 |",
            "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
        ]
        for entry in entries:
            t_name = entry.get("team", {}).get("displayName", "")
            if "Arsenal" in t_name:
                t_name = f"🔴 **{t_name}**"
            st = {s["name"]: s.get("displayValue", s.get("value", "-")) for s in entry.get("stats", [])}
            rk = st.get("rank", "-")
            gp = st.get("gamesPlayed", "-")
            w = st.get("wins", "-")
            d = st.get("ties", "-")
            l = st.get("losses", "-")
            gd = st.get("pointDifferential", "-")
            pts = st.get("points", "-")
            lines.append(f"| {rk} | {t_name} | {gp} | {w} | {d} | {l} | {gd} | **{pts}** |")

        return markdown.markdown("\n".join(lines), extensions=['tables'])
    except Exception as e:
        return f"<p>順位表取得エラー: {e}</p>"

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M")
    next_box_html, stats_html = get_match_and_stats()
    standings_html = get_pl_standings()
    news_html = get_all_uk_news()

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Arsenal Daily Hub</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 700px; margin: 0 auto; padding: 10px; background: #111; color: #eaeaea; font-size: 15px; line-height: 1.55; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #ef0107; padding-bottom: 6px; }}
  .header h1 {{ font-size: 1.2rem; margin: 0; color: #ff4d4d; }}
  .next-box {{ background: #1c1c1c; border-left: 4px solid #ef0107; padding: 8px 12px; margin: 10px 0; font-size: 0.9rem; border-radius: 4px; }}
  .tabs {{ display: flex; gap: 5px; margin: 12px 0; }}
  .tab-btn {{ flex: 1; padding: 9px 4px; background: #222; color: #aaa; border: 1px solid #333; border-radius: 6px; font-weight: bold; font-size: 0.88rem; cursor: pointer; text-align: center; user-select: none; }}
  .tab-btn.active {{ background: #ef0107; color: #fff; border-color: #ef0107; }}
  .content {{ display: none; }}
  .content.active {{ display: block; }}
  h2 {{ font-size: 1.05rem; background: #1e1e1e; padding: 6px 10px; border-left: 3px solid #ef0107; margin: 18px 0 8px; }}
  details {{ background: #181818; border: 1px solid #2a2a2a; border-radius: 6px; margin-bottom: 8px; padding: 8px 10px; }}
  summary {{ font-weight: bold; cursor: pointer; outline: none; list-style: none; }}
  summary::-webkit-details-marker {{ display: none; }}
  .detail-box {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #333; font-size: 0.92rem; color: #ccc; }}
  .detail-box ul {{ padding-left: 18px; margin: 4px 0; }}
  .detail-box li {{ margin-bottom: 4px; }}
  .ext-link {{ text-align: right; margin: 4px 0 0; font-size: 0.8rem; }}
  a {{ color: #66b3ff; text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 0.8rem; }}
  th, td {{ border: 1px solid #2a2a2a; padding: 5px 6px; }}
  th {{ background: #222; }}
  tr:nth-child(even) {{ background: #181818; }}
</style>
</head>
<body>
  <div id="app">
    <div class="header">
      <h1>🔴 Arsenal & PL Hub</h1>
      <small>{jst_now} 更新</small>
    </div>
    {next_box_html}
    <div class="tabs">
      <div class="tab-btn active" onclick="switchTab('news', this)">📰 ニュース</div>
      <div class="tab-btn" onclick="switchTab('stats', this)">📊 前回スタッツ</div>
      <div class="tab-btn" onclick="switchTab('table', this)">🏆 PL順位表</div>
    </div>
    <div id="news" class="content active">{news_html}</div>
    <div id="stats" class="content">{stats_html}</div>
    <div id="table" class="content">{standings_html}</div>
  </div>
<script>
  function switchTab(id, el) {{
    document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
  }}
  try {{
    localStorage.setItem('arsenal_hub_cache', document.getElementById('app').innerHTML);
  }} catch(e) {{}}
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
