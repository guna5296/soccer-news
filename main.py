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
    http_options=types.HttpOptions(timeout=90000)
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

def build_url(*parts):
    raw = "".join(parts)
    raw = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', raw)
    return raw.replace("[", "").replace("]", "")

# ドメイン分割定義
P_HTTPS = "https" + "://"
D_GUARDIAN = "www.theguardian" + ".com"
D_BBC = "feeds.bbci" + ".co.uk"
D_ARSEBLOG = "arseblog" + ".news"
D_FLONDON = "www.football" + ".london"
D_YOUTH = "arsenalyouth.wordpress" + ".com"
D_JUSTARS = "www.justarsenal" + ".com"
D_ESPN = "site.api.espn" + ".com"
D_FOTMOB = "www.fotmob" + ".com"
D_FPL = "fantasy.premierleague" + ".com"

# 女子チームの記事のみを除外（U21/U18は残す）
WOMEN_EXCLUDE_PATTERNS = [
    r"\barsenal women\b", r"\bwsl\b", r"\bwomen's super league\b",
    r"\blionesses\b", r"\barsenal ladies\b", r"\brenee slegers\b",
    r"\bjonas eidevall\b", r"\bwomen's champions league\b", r"\buwcl\b"
]

# ビッグ6判定用キーワード（他14クラブと1:1に均等化するため使用）
BIG6_PATTERNS = [
    r"\bmanchester city\b", r"\bman city\b", r"\bguardiola\b", r"\bhaaland\b",
    r"\bliverpool\b", r"\bslot\b", r"\bsalah\b", r"\banfield\b",
    r"\bchelsea\b", r"\bmaresca\b", r"\bstamford bridge\b", r"\bpalmer\b",
    r"\bmanchester united\b", r"\bman utd\b", r"\bman united\b", r"\bamorim\b", r"\bold trafford\b",
    r"\btottenham\b", r"\bspurs\b", r"\bpostecoglou\b"
]

def is_valid_article(title, summary):
    text = (title + " " + summary).lower()
    for pat in WOMEN_EXCLUDE_PATTERNS:
        if re.search(pat, text):
            return False
    return True

def is_big6_article(title, summary):
    text = (title + " " + summary).lower()
    for pat in BIG6_PATTERNS:
        if re.search(pat, text):
            return True
    return False

# --------------------------------------------------
# 1. Geminiモデル自動検出 & 2分割生成
# --------------------------------------------------
def get_best_model_list():
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
    return candidates

def call_gemini(prompt, candidates):
    last_err = ""
    for model_name in candidates:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=8192)
            )
            if res and res.text:
                cleaned = res.text.replace("```html", "").replace("```", "").strip()
                cleaned = cleaned.replace('target="_blank"', 'target="_blank" rel="noopener noreferrer"')
                return cleaned
        except Exception as e:
            last_err = str(e)
            continue
    return f"<p>要約生成エラー: {last_err}</p>"

def get_all_uk_news():
    seen_titles = set()

    # --- A. アーセナル（トップ＋U21）の収集 ---
    arsenal_feeds = [
        ("Arsenal (Arseblog)", build_url(P_HTTPS, D_ARSEBLOG, "/feed/"), 7),
        ("Arsenal (Football.London)", build_url(P_HTTPS, D_FLONDON, "/arsenal-fc/?service=rss"), 6),
        ("Arsenal (Guardian)", build_url(P_HTTPS, D_GUARDIAN, "/football/arsenal/rss"), 5),
        ("Arsenal (BBC)", build_url(P_HTTPS, D_BBC, "/sport/football/teams/arsenal/rss.xml"), 5),
        ("Arsenal (JustArsenal)", build_url(P_HTTPS, D_JUSTARS, "/feed"), 5),
        ("Arsenal U21/Youth (Jeorge Bird)", build_url(P_HTTPS, D_YOUTH, "/feed/"), 8)
    ]

    ars_data = []
    for name, url, limit in arsenal_feeds:
        try:
            resp = scraper.get(url, timeout=10)
            f = feedparser.parse(resp.content)
            count = 0
            for e in f.entries:
                if count >= limit:
                    break
                title = e.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                raw_html = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
                clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)
                if not is_valid_article(title, clean_text):
                    continue
                seen_titles.add(title)
                ars_data.append(f"[{name}]\nTitle: {title}\nContent: {clean_text[:950]}\nLink: {e.get('link', '')}\n")
                count += 1
        except Exception as e:
            print(f"RSSスキップ ({name}): {e}", flush=True)

    # --- B. PL全般の収集（ビッグ6と他14クラブを1:1で均等確保） ---
    pl_feeds = [
        ("PL (Guardian)", build_url(P_HTTPS, D_GUARDIAN, "/football/premierleague/rss")),
        ("PL (BBC)", build_url(P_HTTPS, D_BBC, "/sport/football/premier-league/rss.xml")),
        ("Football (BBC)", build_url(P_HTTPS, D_BBC, "/sport/football/rss.xml")),
        ("Football (Guardian)", build_url(P_HTTPS, D_GUARDIAN, "/football/rss"))
    ]

    big6_bucket = []
    other14_bucket = []
    gossip_bucket = []

    for name, url in pl_feeds:
        try:
            resp = scraper.get(url, timeout=10)
            f = feedparser.parse(resp.content)
            for e in f.entries:
                title = e.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                raw_html = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
                clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)
                if not is_valid_article(title, clean_text):
                    continue
                # アーセナル単体記事はすでにAで取得済みなのでスキップ
                if "arsenal" in title.lower() and not is_big6_article(title, clean_text):
                    continue

                seen_titles.add(title)
                entry_str = f"[{name}]\nTitle: {title}\nContent: {clean_text[:950]}\nLink: {e.get('link', '')}\n"

                if is_big6_article(title, clean_text):
                    if len(big6_bucket) < 15:
                        big6_bucket.append(entry_str)
                else:
                    if len(other14_bucket) < 16:
                        other14_bucket.append(entry_str)
        except Exception as e:
            print(f"PL RSSスキップ ({name}): {e}", flush=True)

    # 移籍ゴシップ専用枠
    try:
        g_url = build_url(P_HTTPS, D_BBC, "/sport/football/gossip/rss.xml")
        resp = scraper.get(g_url, timeout=10)
        f = feedparser.parse(resp.content)
        for e in f.entries[:2]:
            raw_html = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
            clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)
            gossip_bucket.append(f"[移籍ゴシップ (BBC)]\nTitle: {e.get('title', '')}\nContent: {clean_text[:1500]}\nLink: {e.get('link', '')}\n")
    except Exception:
        pass

    candidates = get_best_model_list()

    # --- 翻訳バッチ1：アーセナル（トップ 10〜12件 ＋ U21/アカデミー 4〜5件 ＝ 他よりちょい多め） ---
    prompt_ars = f"""
あなたはアーセナル専門の熟練ジャーナリストです。
以下の現地英語メディアの最新記事から、**男子トップチーム（10〜12件）**および**U21・U18・アカデミー・若手ローン選手（4〜5件）**の記事を抽出し、指定のHTML構造のみで濃密に日本語要約してください。
※女子チーム（WSL）の話題は除外してください。コードブロック（```html等）は絶対につけないでください。

【出力ルール】
1記事あたり、外部サイトを開かなくても全容が把握できるよう **具体的な発言、戦術・起用背景、スタッツや怪我の状況、今後の見通しを4個の詳しい箇条書き（合計250〜350文字程度）** で濃く記述してください。

<h2>🔴 アーセナル・トップチーム最新ニュース（10〜12件）</h2>
<details>
  <summary>🔴 [メディア名] [具体的で情報量の多い日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>要点・背景：</b>[誰が・いつ・どうしたか、具体的な背景]</li>
      <li><b>発言・引用：</b>[アルテタ監督や選手、現地番記者の具体的なコメント翻訳]</li>
      <li><b>戦術・詳細データ：</b>[ポジション、怪我の状態、戦術的ポイントや契約条件]</li>
      <li><b>今後の見通し：</b>[次節の起用や今後のスケジュールへの影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>🌱 アーセナル U21・U18・アカデミー＆ローン選手動向（4〜5件）</h2>
※「Arsenal U21/Youth (Jeorge Bird)」などの情報から、PL2（U21）やU18の試合結果、得点者、先発メンバー、有望株（プロ契約・トップ昇格候補・ローン組）の最新動向を抽出し、以下の形式で詳しく出力してください：
<details>
  <summary>🌱 [U21/アカデミー] [若手選手名や試合結果がわかる具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>試合結果・動向：</b>[U21/U18の対戦カード、スコア、または契約・ローン情報]</li>
      <li><b>注目選手・スタッツ：</b>[ゴールやアシストを記録した若手選手名、出場メンバー、プレー評価]</li>
      <li><b>トップチームへの展望：</b>[トップ帯同状況や今後の期待値]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

【入力データ】:
{chr(10).join(ars_data)}
"""

    # --- 翻訳バッチ2：ビッグ6（10〜11件）と 他プレミア14クラブ（10〜11件）を完全に半々（1:1）で出力 ---
    pl_combined_input = (
        "=== 【ビッグ6候補記事】 ===\n" + "\n".join(big6_bucket) +
        "\n=== 【その他プレミア14クラブ候補記事】 ===\n" + "\n".join(other14_bucket) +
        "\n=== 【移籍ゴシップ候補記事】 ===\n" + "\n".join(gossip_bucket)
    )

    prompt_pl = f"""
あなたはプレミアリーグ専門の熟練ジャーナリストです。
以下の現地英語メディアの記事群から、**①ビッグ6（アーセナル以外の5クラブ：10〜11件）** と **②その他プレミアリーグ14クラブ（10〜11件）** が **完全に半々（1:1の同数・同ボリューム）** になるように厳選し、さらに **③移籍の噂・ゴシップ（6〜8件）** を加えて、指定のHTML構造のみで濃密に日本語要約してください。
※ビッグ6ばかりを優遇せず、必ずその他14クラブも同じ記事数（10〜11件）しっかり出力してください。
※女子サッカーや他国リーグのみの記事は除外してください。コードブロック（```html等）は絶対につけないでください。

【出力ルール】
1記事あたり **具体的な発言、戦術やクラブ事情、数字や今後の見通しを含めて、4個の詳しい箇条書き（合計250〜350文字程度）** で濃く記述してください。

<h2>⚔️ ビッグ6動向（10〜11件 ※シティ/リヴァプール/チェルシー/Utd/スパーズ）</h2>
<details>
  <summary>⚔️ 【クラブ名】[具体的でわかりやすい日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>要点・背景：</b>[出来事や試合・クラブ状況の具体的な背景]</li>
      <li><b>監督・選手の発言：</b>[具体的なコメント内容の翻訳]</li>
      <li><b>戦術・チーム事情：</b>[怪我人、起用法、スタッツ、経営・処分の詳細]</li>
      <li><b>今後の影響：</b>[順位争いや次節への影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]" target="_blank">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>🦁 その他プレミア14クラブ動向（10〜11件 ※ビッグ6と同数・半々）</h2>
※ニューカッスル、アストン・ヴィラ、ブライトン、ボーンマス、フラム、ブレントフォード、ウェストハム、パレス、エヴァートン、フォレスト、ウルヴス等の記事をビッグ6と同じ件数（10〜11件）抽出し、<details><summary>🦁 【クラブ名】[具体的な日本語タイトル]</summary>... の形式で4つの濃い箇条書きで出力してください。

<h2>🔄 移籍の噂・ゴシップ (BBC Gossip / 現地紙まとめ：6〜8件)</h2>
※アーセナル関連の噂を最優先しつつ、プレミア各クラブの注目ゴシップを <details><summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>... の形式で、移籍金や背景を含めて詳しく出力してください。

【入力データ】:
{pl_combined_input}
"""

    part1_html = call_gemini(prompt_ars, candidates) if ars_data else "<p>アーセナル記事取得なし</p>"
    part2_html = call_gemini(prompt_pl, candidates) if (big6_bucket or other14_bucket) else "<p>PL記事取得なし</p>"
    return part1_html + "\n" + part2_html

# --------------------------------------------------
# 2. 次回日程(JST) ＆ 前回試合スタッツ
# --------------------------------------------------
def get_match_and_stats():
    jst = pytz.timezone("Asia/Tokyo")
    now_utc = datetime.now(timezone.utc)
    next_html = "<div class='next-box'>次回試合予定データ取得中</div>"
    stats_md = "<p>スタッツデータなし</p>"

    fpl_teams = {}
    fpl_last_fix = None
    try:
        boot_url = build_url(P_HTTPS, D_FPL, "/api/bootstrap-static/")
        boot = requests.get(boot_url, headers=HEADERS, timeout=10).json()
        fpl_teams = {t["id"]: t["name"] for t in boot.get("teams", [])}
        ars_id = next((t["id"] for t in boot.get("teams", []) if t["name"] == "Arsenal"), 1)

        fix_url = build_url(P_HTTPS, D_FPL, "/api/fixtures/")
        fixes = requests.get(fix_url, headers=HEADERS, timeout=10).json()
        ars_fixes = [f for f in fixes if f.get("team_h") == ars_id or f.get("team_a") == ars_id]
        
        fpl_up = [f for f in ars_fixes if not f.get("finished") and f.get("kickoff_time")]
        if fpl_up:
            nxt = fpl_up[0]
            utc_dt = datetime.fromisoformat(nxt["kickoff_time"].replace("Z", "+00:00"))
            jst_dt = utc_dt.astimezone(jst)
            h_name = fpl_teams.get(nxt["team_h"], "Home")
            a_name = fpl_teams.get(nxt["team_a"], "Away")
            next_html = (
                f"<div class='next-box'>"
                f"<b>⏰ 次回PL試合: {h_name} vs {a_name}</b><br>"
                f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ (PL 第{nxt.get('event', '-')}節)"
                f"</div>"
            )
        fpl_fin = [f for f in ars_fixes if f.get("finished") or (f.get("team_h_score") is not None)]
        if fpl_fin:
            fpl_last_fix = fpl_fin[-1]
    except Exception as e:
        print(f"FPL fix err: {e}", flush=True)

    fotmob_lines = []
    try:
        fm_team_url = build_url(P_HTTPS, D_FOTMOB, f"/api/teams?id={ARSENAL_FOTMOB_ID}&ccode3=JPN")
        fm_res = scraper.get(fm_team_url, timeout=10)
        if fm_res.status_code == 200:
            fm_data = fm_res.json()
            all_fix = fm_data.get("fixtures", {}).get("allFixtures", {}).get("fixtures", [])
            
            fm_up = [m for m in all_fix if not m.get("status", {}).get("started")]
            if fm_up:
                nxt_fm = fm_up[0]
                utc_dt = datetime.fromisoformat(nxt_fm["status"]["utcTime"].replace("Z", "+00:00"))
                jst_dt = utc_dt.astimezone(jst)
                next_html = (
                    f"<div class='next-box'>"
                    f"<b>⏰ 次回公式戦: {nxt_fm['home']['name']} vs {nxt_fm['away']['name']}</b><br>"
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
                    fotmob_lines.append(f"### ⚽ {h_team} {score_s} {a_team}\n")

                    lineup_data = d_json.get("content", {}).get("lineup", {})
                    ars_players_parsed = []
                    for side in ["homeTeam", "awayTeam"]:
                        t_obj = lineup_data.get(side, {})
                        if str(t_obj.get("id")) == ARSENAL_FOTMOB_ID or "Arsenal" in t_obj.get("name", ""):
                            for p in t_obj.get("starters", []) + t_obj.get("subs", []):
                                p_name = p.get("name", "")
                                pos = p.get("positionStringShort", "-")
                                rating_val = p.get("performance", {}).get("rating", 0) or 0
                                p_stats = {}
                                for st_group in p.get("stats", []):
                                    for st_key, st_val in st_group.get("stats", {}).items():
                                        p_stats[st_key] = st_val.get("stat", {}).get("value", "-")
                                mins = p_stats.get("Minutes played", "-")
                                if mins == "-" and p in t_obj.get("subs", []):
                                    continue
                                g_duel_str = str(p_stats.get("Ground duels won", "0"))
                                g_duel_num = int(re.match(r"(\d+)", g_duel_str).group(1)) if re.match(r"(\d+)", g_duel_str) else 0
                                a_duel_str = str(p_stats.get("Aerial duels won", "0"))
                                ars_players_parsed.append({
                                    "name": p_name, "pos": pos, "rating": float(rating_val),
                                    "mins": mins, "goals": p_stats.get("Goals", 0), "assists": p_stats.get("Assists", 0),
                                    "shots": p_stats.get("Total shots", "-"), "passes": p_stats.get("Accurate passes", "-"),
                                    "g_duel_str": g_duel_str, "g_duel_num": g_duel_num,
                                    "a_duel_str": a_duel_str
                                })

                    if ars_players_parsed and len(ars_players_parsed) >= 3:
                        top_rating = sorted(ars_players_parsed, key=lambda x: x["rating"], reverse=True)[:3]
                        top_g_duel = sorted(ars_players_parsed, key=lambda x: x["g_duel_num"], reverse=True)[:3]
                        fotmob_lines.append("#### 👑 この試合のアーセナル個人ランキング")
                        fotmob_lines.append("| 部門 | 1位 | 2位 | 3位 |")
                        fotmob_lines.append("| :--- | :--- | :--- | :--- |")
                        if top_rating[0]["rating"] > 0:
                            fotmob_lines.append(f"| **最高採点** | {top_rating[0]['name']} ({top_rating[0]['rating']:.1f}) | {top_rating[1]['name']} ({top_rating[1]['rating']:.1f}) | {top_rating[2]['name']} ({top_rating[2]['rating']:.1f}) |")
                        fotmob_lines.append(f"| **地上戦デュエル勝利** | {top_g_duel[0]['name']} ({top_g_duel[0]['g_duel_str']}) | {top_g_duel[1]['name']} ({top_g_duel[1]['g_duel_str']}) | {top_g_duel[2]['name']} ({top_g_duel[2]['g_duel_str']}) |")
                        fotmob_lines.append("")

                        fotmob_lines.append("#### 👥 アーセナル全出場選手 採点＆デュエル一覧")
                        fotmob_lines.append("| 選手 | ポジ | 採点 | 時間 | G/A | シュート | パス成功 | 地上戦勝 | 空中戦勝 |")
                        fotmob_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                        for p in ars_players_parsed:
                            r_str = f"**{p['rating']:.1f}**" if p["rating"] > 0 else "-"
                            ga = f"{p['goals']}G {p['assists']}A" if (p['goals'] or p['assists']) else "-"
                            fotmob_lines.append(f"| **{p['name']}** | {p['pos']} | {r_str} | {p['mins']}' | {ga} | {p['shots']} | {p['passes']} | {p['g_duel_str']} | {p['a_duel_str']} |")
                        fotmob_lines.append("")

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

    espn_lines = []
    try:
        leagues = [
            ("eng.1", "Premier League"),
            ("uefa.champions", "Champions League"),
            ("eng.fa", "FA Cup"),
            ("eng.league_cup", "Carabao Cup")
        ]
        past_events = []
        future_events = []
        for lg_code, lg_name in leagues:
            try:
                url_s = build_url(P_HTTPS, D_ESPN, f"/apis/site/v2/sports/soccer/{lg_code}/teams/{ARSENAL_ESPN_ID}/schedule")
                res_s = requests.get(url_s, headers=HEADERS, timeout=8).json()
                for ev in res_s.get("events", []):
                    ev_dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                    state = ev.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state", "")
                    if state == "post" or ev_dt < now_utc:
                        past_events.append((ev_dt, lg_code, lg_name, ev))
                    elif state == "pre" or ev_dt >= now_utc:
                        future_events.append((ev_dt, lg_code, lg_name, ev))
            except Exception:
                pass

        if future_events and "取得中" in next_html:
            future_events.sort(key=lambda x: x[0])
            f_dt, _, f_lg_name, f_ev = future_events[0]
            jst_dt = f_dt.astimezone(jst)
            teams = f_ev["competitions"][0]["competitors"]
            home = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "home"), "")
            away = next((t["team"]["displayName"] for t in teams if t["homeAway"] == "away"), "")
            next_html = (
                f"<div class='next-box'>"
                f"<b>⏰ 次回公式戦: {home} vs {away}</b><br>"
                f"日本時間: <span style='color:#ffcc00;font-weight:bold;'>{jst_dt.strftime('%Y年%m月%d日(%a) %H:%M')}</span> キックオフ ({f_lg_name})"
                f"</div>"
            )

        if past_events:
            past_events.sort(key=lambda x: x[0])
            _, last_lg_code, last_lg_name, last_ev = past_events[-1]
            last_id = last_ev["id"]

            sum_url = build_url(P_HTTPS, D_ESPN, f"/apis/site/v2/sports/soccer/{last_lg_code}/summary?event={last_id}")
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
            if isinstance(h_score, dict): h_score = h_score.get("displayValue", "0")
            if isinstance(a_score, dict): a_score = a_score.get("displayValue", "0")

            espn_lines.append(f"### ⚽ {h_name} {h_score} - {a_score} {a_name} ({last_lg_name})\n")

            details = comp.get("details", [])
            if details:
                espn_lines.append("#### 🎯 ゴール＆試合イベント詳細")
                espn_lines.append("| 時間 | チーム | 選手 | 内容 |")
                espn_lines.append("| :--- | :--- | :--- | :--- |")
                for d in details:
                    clock = d.get("clock", {}).get("displayValue", "")
                    t_id = str(d.get("team", {}).get("id", ""))
                    t_label = h_name if t_id == h_id else a_name
                    parts = d.get("participants", [])
                    p_name = parts[0].get("athlete", {}).get("displayName", "-") if parts else "-"
                    d_type = d.get("type", {}).get("text", "")
                    espn_lines.append(f"| {clock} | {t_label} | **{p_name}** | {d_type} |")
                espn_lines.append("")

            rosters = sum_res.get("rosters", [])
            for r in rosters:
                if str(r.get("team", {}).get("id", "")) == ARSENAL_ESPN_ID:
                    espn_lines.append("#### 👥 アーセナル出場選手＆個人スタッツ")
                    espn_lines.append("| 選手 | ポジ | 出場 | シュート(枠内) | G / A | タックル / ファウル |")
                    espn_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: |")
                    for ath in r.get("roster", []):
                        starter = "先発" if ath.get("starter") else ("交代" if ath.get("subbedIn") else "")
                        if not starter:
                            continue
                        p_name = ath.get("athlete", {}).get("displayName", "")
                        pos = ath.get("position", {}).get("abbreviation", "-")
                        st_map = {s["name"]: s["displayValue"] for s in ath.get("stats", [])}
                        sh = f"{st_map.get('totalShots', '0')} ({st_map.get('shotsOnTarget', '0')})"
                        ga = f"{st_map.get('totalGoals', '0')}G / {st_map.get('goalAssists', '0')}A"
                        tf = f"T:{st_map.get('totalTackles', '0')} / F:{st_map.get('foulsCommitted', '0')}"
                        espn_lines.append(f"| **{p_name}** | {pos} | {starter} | {sh} | {ga} | {tf} |")
                    espn_lines.append("")

            box_teams = sum_res.get("boxscore", {}).get("teams", [])
            if len(box_teams) >= 2:
                h_stats, a_stats = {}, {}
                for bt in box_teams:
                    bt_id = str(bt.get("team", {}).get("id", ""))
                    st_dict = {s["name"]: s["displayValue"] for s in bt.get("statistics", [])}
                    if bt_id == h_id: h_stats = st_dict
                    elif bt_id == a_id: a_stats = st_dict

                espn_lines.append("#### 📊 チーム詳細スタッツ比較")
                espn_lines.append(f"| 項目 | {h_name} | {a_name} |")
                espn_lines.append("| :--- | :---: | :---: |")
                label_map = [
                    ("possessionPct", "ボール支配率 (%)"), ("totalShots", "総シュート数"),
                    ("shotsOnTarget", "枠内シュート数"), ("shotPct", "枠内シュート率 (%)"),
                    ("blockedShots", "ブロックされたシュート"), ("wonCorners", "コーナーキック"),
                    ("offsides", "オフサイド"), ("totalPasses", "総パス数"),
                    ("accuratePasses", "成功パス数"), ("passPct", "パス成功率 (%)"),
                    ("totalCrosses", "クロス総数"), ("accurateCrosses", "成功クロス数"),
                    ("totalLongBalls", "ロングボール総数"), ("accurateLongBalls", "成功ロングボール数"),
                    ("totalTackles", "タックル総数"), ("effectiveTackles", "タックル成功数"),
                    ("tacklePct", "タックル成功率 (%)"), ("interceptions", "インターセプト"),
                    ("effectiveClearance", "クリア数"), ("saves", "GKセーブ数"),
                    ("foulsCommitted", "ファウル数"), ("yellowCards", "イエローカード"), ("redCards", "レッドカード")
                ]
                for key, jp in label_map:
                    if key in h_stats or key in a_stats:
                        espn_lines.append(f"| {jp} | {h_stats.get(key, '-')} | {a_stats.get(key, '-')} |")
                espn_lines.append("")
    except Exception as e:
        print(f"ESPN stats err: {e}", flush=True)

    if not espn_lines and fpl_last_fix:
        h_n = fpl_teams.get(fpl_last_fix["team_h"], "Home")
        a_n = fpl_teams.get(fpl_last_fix["team_a"], "Away")
        h_s = fpl_last_fix.get("team_h_score", 0)
        a_s = fpl_last_fix.get("team_a_score", 0)
        espn_lines.append(f"### ⚽ {h_n} {h_s} - {a_s} {a_n} (PL 第{fpl_last_fix.get('event')}節)\n")

    if espn_lines:
        stats_md = markdown.markdown("\n".join(espn_lines), extensions=['tables'])

    return next_html, stats_md

# --------------------------------------------------
# 3. PL順位表 ＆ 個人スタッツランキング（FPL公式データ）
# --------------------------------------------------
def get_standings_and_rankings():
    lines = []

    try:
        boot_url = build_url(P_HTTPS, D_FPL, "/api/bootstrap-static/")
        boot = requests.get(boot_url, headers=HEADERS, timeout=10).json()
        teams_map = {t["id"]: t["short_name"] for t in boot.get("teams", [])}
        ars_id = next((t["id"] for t in boot.get("teams", []) if t["name"] == "Arsenal"), 1)
        elements = boot.get("elements", [])

        ars_players = [p for p in elements if p.get("team") == ars_id and p.get("minutes", 0) > 0]
        if ars_players:
            lines.append("### 🔴 アーセナル シーズン個人スタッツ一覧 (PL公式)\n")
            lines.append("| 選手 | 時間 | G | A | xG | xA | 脅威度 | 創造性 | 守備貢献(T&CBI) |")
            lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
            ars_players.sort(key=lambda x: (x.get("goals_scored", 0) + x.get("assists", 0), x.get("minutes", 0)), reverse=True)
            for p in ars_players[:18]:
                name = p.get("web_name", "")
                mins = p.get("minutes", 0)
                g = p.get("goals_scored", 0)
                a = p.get("assists", 0)
                xg = p.get("expected_goals", "0.0")
                xa = p.get("expected_assists", "0.0")
                thr = p.get("threat", "0")
                cre = p.get("creativity", "0")
                def_c = p.get("clearances_blocks_interceptions", 0) + p.get("tackles", 0)
                lines.append(f"| **{name}** | {mins}' | **{g}** | **{a}** | {xg} | {xa} | {thr} | {cre} | {def_c} |")
            lines.append("")

        if elements:
            top_g = sorted(elements, key=lambda x: (x.get("goals_scored", 0), -x.get("minutes", 0)), reverse=True)[:5]
            top_a = sorted(elements, key=lambda x: (x.get("assists", 0), -x.get("minutes", 0)), reverse=True)[:5]
            top_xg = sorted(elements, key=lambda x: float(x.get("expected_goals", 0) or 0), reverse=True)[:5]

            lines.append("### 👑 プレミアリーグ個人ランキング TOP 5\n")
            lines.append("| 順位 | ⚽ 得点王争い | 🎯 アシスト王争い | 🔥 ゴール期待値(xG)上位 |")
            lines.append("| :---: | :--- | :--- | :--- |")
            for i in range(5):
                pg = top_g[i]
                pa = top_a[i]
                px = top_xg[i]
                g_str = f"**{pg['web_name']}** ({teams_map.get(pg['team'], '')}) - **{pg['goals_scored']}G**"
                a_str = f"**{pa['web_name']}** ({teams_map.get(pa['team'], '')}) - **{pa['assists']}A**"
                x_str = f"**{px['web_name']}** ({teams_map.get(px['team'], '')}) - **{px.get('expected_goals', '0')}**"
                lines.append(f"| {i+1}位 | {g_str} | {a_str} | {x_str} |")
            lines.append("")
    except Exception as e:
        print(f"FPL ranking err: {e}", flush=True)

    try:
        url = build_url(P_HTTPS, D_ESPN, "/apis/v2/sports/soccer/eng.1/standings")
        res = requests.get(url, headers=HEADERS, timeout=10).json()
        entries = res.get("children", [{}])[0].get("standings", {}).get("entries", [])
        if entries:
            lines.append("### 🏆 プレミアリーグ 最新順位表\n")
            lines.append("| 順位 | クラブ | 試 | 勝 | 分 | 負 | 得失 | 勝点 |")
            lines.append("| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
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
            lines.append("")
    except Exception as e:
        lines.append(f"順位表取得エラー: {e}")

    return markdown.markdown("\n".join(lines), extensions=['tables'])

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M")
    next_box_html, stats_html = get_match_and_stats()
    standings_html = get_standings_and_rankings()
    news_html = get_all_uk_news()

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>Arsenal Daily Hub</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 720px; margin: 0 auto; padding: 10px; background: #111; color: #eaeaea; font-size: 15px; line-height: 1.6; overscroll-behavior-y: contain; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #ef0107; padding-bottom: 8px; }}
  .header h1 {{ font-size: 1.15rem; margin: 0; color: #ff4d4d; }}
  .reload-btn {{ background: #222; color: #fff; border: 1px solid #444; padding: 5px 10px; border-radius: 6px; font-size: 0.8rem; font-weight: bold; cursor: pointer; }}
  .reload-btn:active {{ background: #ef0107; }}
  #ptr-indicator {{ text-align: center; color: #ffcc00; font-size: 0.85rem; height: 0; overflow: hidden; transition: height 0.2s; }}
  .next-box {{ background: #1c1c1c; border-left: 4px solid #ef0107; padding: 8px 12px; margin: 10px 0; font-size: 0.9rem; border-radius: 4px; }}
  .tabs {{ display: flex; gap: 5px; margin: 12px 0; }}
  .tab-btn {{ flex: 1; padding: 9px 4px; background: #222; color: #aaa; border: 1px solid #333; border-radius: 6px; font-weight: bold; font-size: 0.84rem; cursor: pointer; text-align: center; user-select: none; }}
  .tab-btn.active {{ background: #ef0107; color: #fff; border-color: #ef0107; }}
  .content {{ display: none; min-height: 70vh; overflow-x: auto; }}
  .content.active {{ display: block; }}
  h2 {{ font-size: 1.05rem; background: #1e1e1e; padding: 6px 10px; border-left: 3px solid #ef0107; margin: 18px 0 8px; }}
  h3 {{ font-size: 1rem; color: #ff9999; margin: 16px 0 6px; border-bottom: 1px solid #333; padding-bottom: 4px; }}
  details {{ background: #181818; border: 1px solid #2a2a2a; border-radius: 6px; margin-bottom: 8px; padding: 8px 10px; }}
  summary {{ font-weight: bold; cursor: pointer; outline: none; list-style: none; }}
  summary::-webkit-details-marker {{ display: none; }}
  .detail-box {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #333; font-size: 0.92rem; color: #ddd; }}
  .detail-box ul {{ padding-left: 18px; margin: 4px 0; }}
  .detail-box li {{ margin-bottom: 6px; }}
  .ext-link {{ text-align: right; margin: 4px 0 0; font-size: 0.8rem; }}
  a {{ color: #66b3ff; text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 0.78rem; }}
  th, td {{ border: 1px solid #2a2a2a; padding: 5px 6px; }}
  th {{ background: #222; }}
  tr:nth-child(even) {{ background: #181818; }}
</style>
</head>
<body>
  <div id="ptr-indicator">⬇️ 指を離すと最新データに更新します...</div>
  <div id="app">
    <div class="header">
      <h1>🔴 Arsenal & PL Hub</h1>
      <button class="reload-btn" onclick="forceReload()">🔄 {jst_now} 更新</button>
    </div>
    {next_box_html}
    <div class="tabs">
      <div class="tab-btn active" id="btn-news" onclick="switchTab(0)">📰 ニュース＆U21</div>
      <div class="tab-btn" id="btn-stats" onclick="switchTab(1)">📊 前回試合スタッツ</div>
      <div class="tab-btn" id="btn-table" onclick="switchTab(2)">🏆 順位＆個人ランク</div>
    </div>
    <div id="news" class="content active">{news_html}</div>
    <div id="stats" class="content">{stats_html}</div>
    <div id="table" class="content">{standings_html}</div>
  </div>
<script>
  const tabIds = ['news', 'stats', 'table'];
  const btnIds = ['btn-news', 'btn-stats', 'btn-table'];
  let currentTab = 0;

  function switchTab(index) {{
    currentTab = index;
    tabIds.forEach((id, i) => {{
      document.getElementById(id).classList.toggle('active', i === index);
      document.getElementById(btnIds[i]).classList.toggle('active', i === index);
    }});
    window.scrollTo(0, 0);
  }}

  function forceReload() {{
    const btn = document.querySelector('.reload-btn');
    btn.innerText = '⌛ 読込中...';
    window.location.replace(window.location.pathname + '?t=' + new Date().getTime());
  }}

  let startX = 0, startY = 0, isPulling = false, touchedTable = false;
  const ptr = document.getElementById('ptr-indicator');

  document.addEventListener('touchstart', e => {{
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    isPulling = (window.scrollY === 0);
    touchedTable = Boolean(e.target.closest('table'));
  }}, {{passive: true}});

  document.addEventListener('touchmove', e => {{
    if (!isPulling) return;
    const diffY = e.touches[0].clientY - startY;
    const diffX = Math.abs(e.touches[0].clientX - startX);
    if (diffY > 65 && diffY > diffX * 2) {{
      ptr.style.height = '32px';
    }}
  }}, {{passive: true}});

  document.addEventListener('touchend', e => {{
    const endX = e.changedTouches[0].clientX;
    const endY = e.changedTouches[0].clientY;
    const diffX = endX - startX;
    const diffY = endY - startY;

    if (isPulling && diffY > 90 && Math.abs(diffX) < 50) {{
      ptr.innerText = '🔄 更新しています...';
      forceReload();
      return;
    }}
    ptr.style.height = '0px';

    if (!touchedTable && Math.abs(diffX) > 75 && Math.abs(diffX) > Math.abs(diffY) * 1.8) {{
      if (diffX < 0 && currentTab < 2) switchTab(currentTab + 1);
      if (diffX > 0 && currentTab > 0) switchTab(currentTab - 1);
    }}
  }}, {{passive: true}});
</script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
