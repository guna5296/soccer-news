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
    raise ValueError("GEMINI_API_KEY が GitHub Secrets に設定されていません。")

client = genai.Client(api_key=api_key)
ARSENAL_TEAM_ID = 9825

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# コピー時にMarkdownリンク化されるのを防ぐためプロトコルを結合
HTTPS = "https://"

def get_all_uk_news():
    feeds = {
        "Arsenal (Guardian)": (HTTPS + "www.theguardian.com/football/arsenal/rss", 8),
        "Arsenal (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml", 6),
        "Premier League (Guardian)": (HTTPS + "www.theguardian.com/football/premierleague/rss", 10),
        "Transfer Gossip (BBC)": (HTTPS + "feeds.bbci.co.uk/sport/football/gossip/rss.xml", 1)
    }
    
    raw_data = []
    for name, (url, limit) in feeds.items():
        try:
            f = feedparser.parse(url)
            for e in f.entries[:limit]:
                clean_summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text()
                raw_data.append(f"[{name}]\nTitle: {e.title}\nContent: {clean_summary}\nLink: {e.link}\n")
        except Exception as e:
            print(f"RSS error ({name}): {e}")

    prompt = f"""
あなたはアーセナルとプレミアリーグ専門の翻訳編集者です。
以下のイギリスのサッカーニュース群を日本語に翻訳・詳細要約し、指定のHTMLタグ構造で出力してください。
※Markdownのコードブロック（```html など）は絶対につけず、純粋なHTMLタグのみを出力してください。

<h2>🔴 アーセナル最新ニュース（最優先）</h2>
・アーセナルに関する記事をすべて抽出し、1記事ずつ以下の形式で出力：
<details>
  <summary>🔴 [魅力的でわかりやすい日本語タイトル]</summary>
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
・マンチェスターC、リヴァプール、チェルシー、マンチェスターU、トッテナム等の主要ニュースを <details><summary>⚔️ 【クラブ名】[日本語タイトル]</summary>... の形式で出力。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip等)</h2>
・移籍の噂を <details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し]</summary>... の形式で出力。

【入力データ】:
{chr(10).join(raw_data)}
"""
    try:
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return res.text.replace("```html", "").replace("```", "").strip()
    except Exception as e:
        return f"<p>ニュース要約エラー: {e}</p>"

def get_fotmob_data():
    next_html = "<div class='next-box'>次回日程データ取得中または予定なし</div>"
    last_md = "スタッツデータを取得できませんでした。"
    
    try:
        team_url = f"{HTTPS}[www.fotmob.com/api/teams?id=](https://www.fotmob.com/api/teams?id=){ARSENAL_TEAM_ID}&ccode3=JPN"
        res = requests.get(team_url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return next_html, f"<p>FotMob API ステータス: {res.status_code}</p>"
            
        team_res = res.json()
        all_fixtures = team_res.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
        
        finished = [m for m in all_fixtures if m.get("status", {}).get("finished")]
        upcoming = [m for m in all_fixtures
