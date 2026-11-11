import os
import re
import json
import requests
import feedparser
from datetime import datetime
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

HTTPS = "https://"
ARSENAL_ESPN_ID = "359"
ARSENAL_FOTMOB_ID = "9825"

# --------------------------------------------------
# 1. 確実に動くGeminiモデルを自動試行して翻訳・要約
# --------------------------------------------------
def generate_with_best_model(prompt):
    # APIから現在有効なモデル一覧を取得し、新しいFlash系を優先リスト化
    candidates = ["gemini-3.8-flash", "gemini-3.0-flash", "gemini-3-flash"]
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            methods = getattr(m, "supported_actions", []) or getattr(m, "supported_generation_methods", [])
            if methods and "generateContent" not in methods:
                continue
            if "flash" in name and not any(x in name for x in ["1.5", "2.0", "2.5", "vision", "audio", "tts", "8b"]):
                if name not in candidates:
                    candidates.insert(0, name)
            elif "flash" in name and not any(x in name for x in ["1.5", "vision", "audio", "tts"]):
                if name not in candidates:
                    candidates.append(name)
    except Exception as e:
        print(f"モデルリスト取得スキップ: {e}", flush=True)

    last_err = ""
    for model_name in candidates:
        try:
            print(f"Gemini試行中: {model_name} ...", flush=True)
            res = client.models.generate_content(model=model_name, contents=prompt)
            if res and res.text:
                print(f"Gemini生成成功: {model_name}", flush=True)
                return res.text.replace("```html", "").replace("```", "").strip()
        except Exception as e:
            print(f"モデル {model_name} 失敗: {e}", flush=True)
            last_err = str(e)
            continue

    return f"<p>ニュース要約エラー（全モデル試行失敗）: {last_err}</p>"

def get_all_uk_news():
    feeds = {
        "Arsenal (Guardian)": (HTTPS + "www.theguardian.com/football/arsenal/rss", 7),
        "Arsenal (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml", 5),
        "Premier League (Guardian)": (HTTPS + "www.theguardian.com/football/premierleague/rss", 6),
        "Premier League (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/premier-league/rss.xml", 5),
        "Transfer Gossip (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/gossip/rss.xml", 1)
    }
    
    raw_data = []
    seen_titles = set()
    for name, (url, limit) in feeds.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            f = feedparser.parse(resp.content)
            count = 0
            for e in f.entries:
                if count >= limit or e.title in seen_titles:
                    continue
                seen_titles.add(e.title)
                clean_summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()
                raw_data.append(f"[{name}]\nTitle: {e.title}\nSummary: {clean_summary[:400]}\nLink: {e.link}\n")
                count += 1
        except Exception as e:
            print(f"RSS取得スキップ ({name}): {e}", flush=True)

    if not raw_data:
        return "<p>本日の英語ニュースフィードを取得できませんでした。</p>"

    prompt = f"""
あなたはアーセナルとプレミアリーグ専門の翻訳編集者です。
以下のイギリスのサッカーニュース群を日本語に翻訳・詳細要約し、指定のHTMLタグ構造のみで出力してください。
※Markdownのコードブロック（```html等）は絶対につけないでください。

<h2>🔴 アーセナル最新ニュース（最優先）</h2>
アーセナルに関する記事をすべて抽出し、1記事ずつ以下の折りたたみ形式で出力：
<details>
  <summary>🔴 [わかりやすい日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li>[記事の具体的な内容・発言・背景1（詳しく）]</li>
      <li>[翻訳ポイント2（詳しく）]</li>
      <li>[翻訳ポイント3（戦術や今後の見通しなど）]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※外部通信発生</a></p>
  </div>
</details>

<h2>⚔️ ビッグ6＆プレミアリーグ動向</h2>
マンチェスターC、リヴァプール、チェルシー、マンチェスターU、トッテナム等の主要記事を <details><summary>⚔️ 【クラブ名】[日本語タイトル]</summary>... の形式で出力。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip等)</h2>
移籍の噂を <details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>... の形式で出力。

【入力データ】:
{chr(10).join(raw_data)}
"""
    return generate_with_best_model(prompt)

# --------------------------------------------------
# 2. 全公式戦（PL・CL・カップ戦）の次回日程＆前回詳細スタッツ取得
# --------------------------------------------------
def get_match_and_stats():
    jst = pytz.timezone("Asia/Tokyo")
    next_html = "<div class='next-box'>次回試合予定データ取得中</div>"
    stats_md = "<p>スタッツデータなし</p>"

    # --- A. FPL公式API ＋ ESPN複数大会（PL/CL/FA杯/EFL杯）から次回日程と直近試合を収集 ---
    upcoming_list = []
    finished_list = []

    # 1) ESPNの各大会スケジュールを巡回（PL: eng.1, CL: uefa.champions, FA杯: eng.fa, カラバオ杯: eng.league_cup）
    leagues = [
        ("eng.1", "Premier League"),
        ("uefa.champions", "Champions League"),
        ("eng.fa", "FA Cup"),
        ("eng.league_cup", "Carabao Cup")
    ]
    for lg_code, lg_name in leagues:
        try:
            url = f"{HTTPS}[site.api.espn.com/apis/site/v2/sports/soccer/](https://site.api.espn.com/apis/site/v2/sports/soccer/){lg_code}/teams/{ARSENAL_ESPN_ID}/schedule"
            res = requests.get(url, headers=HEADERS, timeout=8).json()
            for ev in res.get("events", []):
                comp = ev.get("competitions", [{}])[0]
                st_type = comp.get("status", {}).get("type", {})
                state = st_type.get("state", "")
                completed = st_type.get("completed", False)
                ev["_lg_code"] = lg_code
                ev["_lg_name"] = lg_name
                if completed or state == "post":
                    finished_list.append(ev)
                elif state == "pre":
                    upcoming_list.append(ev)
        except Exception:
            pass

    # 2) もしESPNのscheduleに未来のPL試合が入っていない場合はFPL公式APIから補完
    if not upcoming_list:
        try:
            boot = requests.get(f"{HTTPS}[fantasy.premierleague.com/api/bootstrap-static/](https://fantasy.premierleague.com/api/bootstrap-static/)", headers=HEADERS, timeout=8).json()
            t_map = {t["id"]: t["name"] for t in boot.get("teams", [])}
            ars_id = next((t["id"] for t in boot.get("teams", []) if t["name"] == "Arsenal"), 1)
            fixes = requests.get(f"{HTTPS}[fantasy.premierleague.com/api/fixtures/](https://fantasy.premierleague.com/api/fixtures/)", headers=HEADERS, timeout=8).json()
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
            print(f"FPL補完エラー: {e}", flush=True)
    else:
        upcoming_list.sort(key=lambda x: x.get("date", ""))
        nxt = upcoming_list[0]
        utc_dt = datetime.fromisoformat(nxt["date"].replace("Z", "+00:00"))
        jst_dt = utc_dt.astimezone(jst)
        teams = nxt["competitions"][0]["competitors"]
        home = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "home"), "")
        away = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "away"), "")
        next_html = (
            f"<div class='next-box'>"
            f"<b>⏰ 次回試合: {home} vs {away}</b><br>"
            f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ ({nxt['_lg_name']})"
            f"</div>"
        )

    # --- B. 直近試合のxG・シュートマップ（Understat）＋ 全項目スタッツ（ESPN）の取得 ---
    xg_lines = []
    try:
        # Understatからアーセナル最新試合のxGとシュート詳細を取得（ブロックされない軽量ルート）
        us_year = datetime.now().year if datetime.now().month >= 8 else datetime.now().year - 1
        us_url = f"{HTTPS}[understat.com/team/Arsenal/](https://understat.com/team/Arsenal/){us_year}"
        us_res = requests.get(us_url, headers=HEADERS, timeout=8)
        if us_res.status_code == 200:
            m = re.search(r"datesData\s*=\s*JSON\.parse\('([^']+)'\)", us_res.text)
            if m:
                decoded = m.group(1).encode('utf-8').decode('unicode_escape')
                matches = json.loads(decoded)
                played = [x for x in matches if x.get("isResult")]
                if played:
                    last_us = played[-1]
                    h_t = last_us["h"]["title"]
                    a_t = last_us["a"]["title"]
                    h_xg = float(last_us["xG"]["h"])
                    a_xg = float(last_us["xG"]["a"])
                    xg_lines.append("#### 🎯 ゴール期待値 (xG - Understat算出)")
                    xg_lines.append(f"| 指標 | {h_t} | {a_t} |")
                    xg_lines.append("| :--- | :---: | :---: |")
                    xg_lines.append(f"| **ゴール期待値 (xG)** | **{h_xg:.2f}** | **{a_xg:.2f}** |")
                    xg_lines.append("")
    except Exception as e:
        print(f"xG取得スキップ: {e}", flush=True)

    if finished_list:
        try:
            finished_list.sort(key=lambda x: x.get("date", ""))
            last_ev = finished_list[-1]
            last_id = last_ev["id"]
            lg_code = last_ev["_lg_code"]
            lg_name = last_ev["_lg_name"]

            sum_url = f"{HTTPS}[site.api.espn.com/apis/site/v2/sports/soccer/](https://site.api.espn.com/apis/site/v2/sports/soccer/){lg_code}/summary?event={last_id}"
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
            
            lines = [f"### {h_name} {h_score} - {a_score} {a_name} ({lg_name})\n"]
            if xg_lines:
                lines.extend(xg_lines)

            # ゴール・カード等の全イベント（時間・選手・詳細）
            details = comp.get("details", [])
            if details:
                lines.append("#### ⚽ ゴール＆試合イベント詳細")
                lines.append("| 時間 | チーム | 選手 | イベント内容 |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for d in details:
                    clock = d.get("clock", {}).get("displayValue", "")
                    t_id = str(d.get("team", {}).get("id", ""))
                    t_label = h_name if t_id == h_id else a_name
                    participants = d.get("participants", [])
                    p_name = participants[0].get("athlete", {}).get("displayName", "-") if participants else "-"
                    d_type = d.get("type", {}).get("text", "")
                    lines.append(f"| {clock} | {t_label} | **{p_name}** | {d_type} |")
                lines.append("")

            # チーム詳細スタッツ（ID照合でホーム/アウェイ逆転を完全防止）
            box_teams = sum_res.get("boxscore", {}).get("teams", [])
            if len(box_teams) >= 2:
                h_stats = {}
                a_stats = {}
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
                    ("crossPct", "クロス成功率 (%)"),
                    ("totalLongBalls", "ロングボール総数"),
                    ("accurateLongBalls", "成功ロングボール数"),
                    ("longBallPct", "ロングボール成功率 (%)"),
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
            stats_md = f"<p>スタッツ取得エラー: {e}</p>"

    return next_html, stats_md

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M")
    next_box_html, stats_html = get_match_and_stats()
    news_html = get_all_uk_news()

    # オフラインキャッシュ（localStorage保存）付きの超軽量HTML
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Arsenal Daily Hub</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 680px; margin: 0 auto; padding: 10px; background: #111; color: #eaeaea; font-size: 15px; line-height: 1.55; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #ef0107; padding-bottom: 6px; }}
  .header h1 {{ font-size: 1.2rem; margin: 0; color: #ff4d4d; }}
  .next-box {{ background: #1c1c1c; border-left: 4px solid #ef0107; padding: 8px 12px; margin: 10px 0; font-size: 0.9rem; border-radius: 4px; }}
  .tabs {{ display: flex; gap: 6px; margin: 12px 0; }}
  .tab-btn {{ flex: 1; padding: 10px; background: #222; color: #aaa; border: 1px solid #333; border-radius: 6px; font-weight: bold; cursor: pointer; text-align: center; user-select: none; }}
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
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 0.82rem; }}
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
      <div class="tab-btn active" onclick="switchTab('news', this)">📰 ニュース＆移籍</div>
      <div class="tab-btn" onclick="switchTab('stats', this)">📊 前回試合スタッツ</div>
    </div>
    <div id="news" class="content active">{news_html}</div>
    <div id="stats" class="content">{stats_html}</div>
  </div>
<script>
  function switchTab(id, el) {{
    document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
  }}
  // 家のWi-Fiで開いた際にスマホ本体へ自動保存（オフライン対策）
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
