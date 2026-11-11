import os
import requests
import feedparser
from datetime import datetime
import pytz
import markdown
from google import genai
from bs4 import BeautifulSoup

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
ARSENAL_TEAM_ID = 9825
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# --------------------------------------------------
# 1. 英紙RSSから記事本文を取得してGeminiで折りたたみHTML化
# --------------------------------------------------
def get_all_uk_news():
    feeds = {
        "Arsenal (Guardian)": ("https://www.theguardian.com/football/arsenal/rss", 8),
        "Arsenal (BBC)": ("https://feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml", 6),
        "Premier League (Guardian)": ("https://www.theguardian.com/football/premierleague/rss", 10),
        "Transfer Gossip (BBC)": ("https://feeds.bbci.co.uk/sport/football/gossip/rss.xml", 1)
    }
    
    raw_data = []
    for name, (url, limit) in feeds.items():
        f = feedparser.parse(url)
        for e in f.entries[:limit]:
            # HTMLタグを除去してテキスト量を確保
            clean_summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()
            raw_data.append(f"[{name}]\nTitle: {e.title}\nContent: {clean_summary}\nLink: {e.link}\n")

    prompt = f"""
あなたはアーセナルとプレミアリーグ専門の翻訳編集者です。
以下のイギリスのサッカーニュース群を日本語に翻訳・詳細要約し、指定のHTMLタグ構造で出力してください。
※Markdownのコードブロック（```html など）は絶対につけず、純粋なHTMLタグのみを出力してください。

【出力構成とルール】
以下の3つの見出し（<h2>）を作り、各ニュースは必ず <details><summary> タグを使った折りたたみ形式で出力してください。
こうすることで、ユーザーが外出先でタップしても通信量（ギガ）を一切消費せずに詳細が読めます。

<h2>🔴 アーセナル最新ニュース（最優先）</h2>
・アーセナルに関する記事をすべて抽出し、1記事ずつ以下の形式で出力：
<details>
  <summary>🔴 [魅力的でわかりやすい日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li>[記事の具体的な内容・背景・発言の翻訳ポイント1（詳しく）]</li>
      <li>[翻訳ポイント2（詳しく）]</li>
      <li>[翻訳ポイント3（戦術や今後の影響など）]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>⚔️ ビッグ6＆プレミアリーグ動向</h2>
・マンチェスターC、リヴァプール、チェルシー、マンチェスターU、トッテナム等の主要ニュースを抽出し、同じく <details><summary>⚔️ 【クラブ名】[日本語タイトル]</summary>... の形式で詳しく3行箇条書きで出力。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip等)</h2>
・移籍の噂をクラブごとに整理し、<details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>... の形式で内容を詳しく出力。

【入力データ】:
{chr(10).join(raw_data)}
"""
    try:
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = res.text.strip()
        # 万が一コードブロックが含まれていた場合は除去
        text = text.replace("```html", "").replace("```", "").strip()
        return text
    except Exception as e:
        return f"<p>ニュース要約エラー: {e}</p>"

# --------------------------------------------------
# 2. FotMobから次回日程(JST)と前回試合の全詳細スタッツ取得
# --------------------------------------------------
def get_fotmob_data():
    team_url = f"[https://www.fotmob.com/api/teams?id=](https://www.fotmob.com/api/teams?id=){ARSENAL_TEAM_ID}&ccode3=JPN"
    team_res = requests.get(team_url, headers=HEADERS).json()
    all_fixtures = team_res.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
    
    finished = [m for m in all_fixtures if m.get("status", {}).get("finished")]
    upcoming = [m for m in all_fixtures if not m.get("status", {}).get("started")]
    
    last_match = finished[-1] if finished else None
    next_match = upcoming[0] if upcoming else None
    jst = pytz.timezone("Asia/Tokyo")

    # 次回日程（日本時間）
    next_html = "<div class='next-box'>次回予定なし</div>"
    if next_match:
        utc_dt = datetime.fromisoformat(next_match["status"]["utcTime"].replace("Z", "+00:00"))
        jst_dt = utc_dt.astimezone(jst)
        home = next_match["home"]["name"]
        away = next_match["away"]["name"]
        comp = next_match.get("tournament", {}).get("name", "")
        next_html = (
            f"<div class='next-box'>"
            f"<b>⏰ 次回試合: {home} vs {away}</b><br>"
            f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ ({comp})"
            f"</div>"
        )

    # 前回試合の超詳細スタッツ
    last_md = "データなし"
    if last_match:
        match_id = last_match["id"]
        detail_url = f"[https://www.fotmob.com/api/matchDetails?matchId=](https://www.fotmob.com/api/matchDetails?matchId=){match_id}&lang=ja"
        detail_res = requests.get(detail_url, headers=HEADERS).json()
        
        home_team = detail_res["general"]["homeTeam"]["name"]
        away_team = detail_res["general"]["awayTeam"]["name"]
        score_str = detail_res["header"]["status"]["scoreStr"]
        
        lines = [f"### {home_team} {score_str} {away_team}\n"]
        
        # シュート詳細 (Shotmap)
        shots = detail_res.get("content", {}).get("shotmap", {}).get("shots", [])
        if shots:
            lines.append("#### 🎯 シュート＆ゴール詳細 (Shotmap)")
            lines.append("| 分 | 選手 | 結果 | 部位 | 状況 | xG | xGOT |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for s in shots:
                is_goal = s.get("eventType") == "Goal"
                res_mark = "⚽ **ゴール**" if is_goal else s.get("eventType", "")
                xg = f"{s.get('expectedGoals', 0):.2f}" if s.get("expectedGoals") else "-"
                xgot = f"{s.get('expectedGoalsOnTarget', 0):.2f}" if s.get("expectedGoalsOnTarget") else "-"
                lines.append(f"| {s.get('min')}' | **{s.get('playerName')}** | {res_mark} | {s.get('shotType')} | {s.get('situation')} | {xg} | {xgot} |")
            lines.append("")

        # 全カテゴリ詳細スタッツ
        stats_groups = detail_res.get("content", {}).get("stats", {}).get("Periods", {}).get("All", {}).get("stats", [])
        for group in stats_groups:
            lines.append(f"#### 📊 {group.get('title', 'スタッツ')}")
            lines.append(f"| 項目 | {home_team} | {away_team} |")
            lines.append("| :--- | :---: | :---: |")
            for item in group.get("stats", []):
                if item.get("type") == "title":
                    continue
                vals = item.get("stats", ["-", "-"])
                if len(vals) >= 2 and vals[0] is not None:
                    lines.append(f"| {item.get('title', '')} | {vals[0]} | {vals[1]} |")
            lines.append("")
            
        last_md = "\n".join(lines)

    return next_html, markdown.markdown(last_md, extensions=['tables'])

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%m/%d %H:%M")
    next_box_html, stats_html = get_fotmob_data()
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
