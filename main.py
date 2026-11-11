import os
import requests
import feedparser
from datetime import datetime
import pytz
import markdown
from google import genai
from bs4 import BeautifulSoup

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is missing in GitHub Secrets.")

client = genai.Client(api_key=api_key)
ARSENAL_ESPN_ID = 359

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

HTTPS = "https://"

# --------------------------------------------------
# 1. 英紙RSS取得 & Gemini要約（折りたたみHTML）
# --------------------------------------------------
def get_all_uk_news():
    feeds = {
        "Arsenal (Guardian)": (HTTPS + "www.theguardian.com/football/arsenal/rss", 8),
        "Arsenal (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml", 6),
        "Premier League (Guardian)": (HTTPS + "www.theguardian.com/football/premierleague/rss", 8),
        "Transfer Gossip (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/gossip/rss.xml", 1)
    }
    
    raw_data = []
    for name, info in feeds.items():
        try:
            f = feedparser.parse(info[0])
            for e in f.entries[:info[1]]:
                clean_summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()
                raw_data.append(f"[{name}]\nTitle: {e.title}\nContent: {clean_summary}\nLink: {e.link}\n")
        except Exception as e:
            print(f"RSS error: {e}")

    prompt = f"""
あなたはアーセナルとプレミアリーグ専門の翻訳編集者です。
以下のイギリスのサッカーニュース群を日本語に翻訳・詳細要約し、指定のHTMLタグ構造のみで出力してください。
※```html などのコードブロックは絶対につけないでください。

<h2>🔴 アーセナル最新ニュース（最優先）</h2>
アーセナルに関する記事をすべて抽出し、1記事ずつ以下の形式で出力：
<details>
  <summary>🔴 [日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li>[翻訳ポイント1（詳しく）]</li>
      <li>[翻訳ポイント2（詳しく）]</li>
      <li>[翻訳ポイント3（詳しく）]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>⚔️ ビッグ6＆プレミアリーグ動向</h2>
主要クラブの動向を <details><summary>⚔️ 【クラブ名】[日本語タイトル]</summary>... の形式で出力。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip等)</h2>
移籍の噂を <details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し]</summary>... の形式で出力。

【入力データ】:
{chr(10).join(raw_data)}
"""
    # 画面のエラーで指定された gemini-3.8-flash を最優先
    for model_name in ["gemini-3.8-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
        try:
            res = client.models.generate_content(model=model_name, contents=prompt)
            return res.text.replace("```html", "").replace("```", "").strip()
        except Exception as e:
            last_err = e
            continue
    return f"<p>ニュース要約エラー: {last_err}</p>"

# --------------------------------------------------
# 2. ESPNパブリックAPIから次回日程(JST)と詳細スタッツ取得（即時完了）
# --------------------------------------------------
def get_match_and_stats():
    jst = pytz.timezone("Asia/Tokyo")
    next_html = "<div class='next-box'>次回予定なし</div>"
    stats_md = "<p>スタッツデータなし</p>"

    try:
        # スケジュール取得
        sched_url = f"{HTTPS}[site.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/teams/){ARSENAL_ESPN_ID}/schedule"
        res = requests.get(sched_url, headers=HEADERS, timeout=5).json()
        events = res.get("events", [])
        
        finished_ev = [ev for ev in events if ev.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "post"]
        upcoming_ev = [ev for ev in events if ev.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state") == "pre"]

        # 次回日程（日本時間）
        if upcoming_ev:
            nxt = upcoming_ev[0]
            utc_dt = datetime.fromisoformat(nxt["date"].replace("Z", "+00:00"))
            jst_dt = utc_dt.astimezone(jst)
            teams = nxt["competitions"][0]["competitors"]
            home = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "home"), "")
            away = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "away"), "")
            next_html = (
                f"<div class='next-box'>"
                f"<b>⏰ 次回試合: {home} vs {away}</b><br>"
                f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ (Premier League)"
                f"</div>"
            )

        # 直近試合のスタッツ詳細
        if finished_ev:
            last_ev_id = finished_ev[-1]["id"]
            sum_url = f"{HTTPS}[site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=](https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?event=){last_ev_id}"
            sum_res = requests.get(sum_url, headers=HEADERS, timeout=5).json()
            
            comp = sum_res["header"]["competitions"][0]
            t_home = next(t for t in comp["competitors"] if t["homeAway"] == "home")
            t_away = next(t for t in comp["competitors"] if t["homeAway"] == "away")
            h_name = t_home["team"]["displayName"]
            a_name = t_away["team"]["displayName"]
            h_score = t_home.get("score", "0")
            a_score = t_away.get("score", "0")
            
            lines = [f"### {h_name} {h_score} - {a_score} {a_name}\n"]
            
            # ゴール＆カード
            details = comp.get("details", [])
            if details:
                lines.append("#### ⚽ ゴール＆主要イベント")
                lines.append("| 時間 | チーム | 選手 | 内容 |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for d in details:
                    clock = d.get("clock", {}).get("displayValue", "")
                    t_id = d.get("team", {}).get("id")
                    t_label = h_name if str(t_id) == str(t_home["id"]) else a_name
                    p_name = d.get("participants", [{}])[0].get("athlete", {}).get("displayName", "-")
                    d_type = d.get("type", {}).get("text", "")
                    lines.append(f"| {clock} | {t_label} | **{p_name}** | {d_type} |")
                lines.append("")

            # チームスタッツ全項目比較
            box_teams = sum_res.get("boxscore", {}).get("teams", [])
            if len(box_teams) >= 2:
                lines.append("#### 📊 試合詳細スタッツ")
                lines.append(f"| 項目 | {h_name} | {a_name} |")
                lines.append("| :--- | :---: | :---: |")
                h_stats = {s["name"]: s["displayValue"] for s in box_teams[0].get("statistics", [])}
                a_stats = {s["name"]: s["displayValue"] for s in box_teams[1].get("statistics", [])}
                
                label_map = {
                    "possessionPct": "ボール支配率 (%)",
                    "totalShots": "総シュート数",
                    "shotsOnTarget": "枠内シュート数",
                    "wonCorners": "コーナーキック",
                    "foulsCommitted": "ファウル数",
                    "yellowCards": "イエローカード",
                    "redCards": "レッドカード",
                    "saves": "GKセーブ数",
                    "totalPasses": "総パス数",
                    "accuratePasses": "成功パス数",
                    "passPct": "パス成功率 (%)",
                    "totalCrosses": "クロス総数",
                    "accurateCrosses": "成功クロス数",
                    "totalLongBalls": "ロングボール総数",
                    "accurateLongBalls": "成功ロングボール数",
                    "totalTackles": "タックル総数",
                    "effectiveTackles": "タックル成功数",
                    "interceptions": "インターセプト",
                    "effectiveClearance": "クリア数"
                }
                for key, jp in label_map.items():
                    if key in h_stats or key in a_stats:
                        lines.append(f"| {jp} | {h_stats.get(key, '-')} | {a_stats.get(key, '-')} |")
                lines.append("")
                
            stats_md = markdown.markdown("\n".join(lines), extensions=['tables'])
    except Exception as e:
        stats_md = f"<p>スタッツ取得エラー: {e}</p>"

    return next_html, stats_md

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%m/%d %H:%M")
    next_box_html, stats_html = get_match_and_stats()
    news_html = get_all_uk_news()

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
<script>
  function switchTab(id, el) {{
    document.querySelectorAll('.content').forEach(c => c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
  }}
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
