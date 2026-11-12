import os
import re
import json
import requests
import cloudscraper
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import pytz
import markdown
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

print("1. スクリプト開始...", flush=True)

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is missing in GitHub Secrets.")

client = genai.Client(
    api_key=api_key,
    http_options=types.HttpOptions(timeout=50000)
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

WOMEN_EXCLUDE_PATTERNS = [
    r"\barsenal women\b", r"\bwsl\b", r"\bwomen's super league\b",
    r"\blionesses\b", r"\barsenal ladies\b", r"\brenee slegers\b",
    r"\bjonas eidevall\b", r"\bwomen's champions league\b", r"\buwcl\b"
]

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
# HTMLの閉じタグ破損を自動修復し、他タブの消失を100%防ぐ関数
# --------------------------------------------------
def sanitize_html_fragment(raw_text):
    if not raw_text:
        return ""
    cleaned = raw_text.replace("```html", "").replace("```", "").strip()
    # 万が一途中で切れて最後の <details> が </details> で閉じていない場合、半端な末尾を切り落とす
    last_open = cleaned.rfind("<details")
    last_close = cleaned.rfind("</details>")
    if last_open != -1 and (last_close == -1 or last_open > last_close):
        cleaned = cleaned[:last_open].strip()
    # BeautifulSoupで閉じ忘れタグを完全修復
    soup = BeautifulSoup(f"<div>{cleaned}</div>", "html.parser")
    for a in soup.find_all("a"):
        a["target"] = "_blank"
        a["rel"] = "noopener noreferrer"
    inner = "".join(str(child) for child in soup.div.contents) if soup.div else cleaned
    return inner

# --------------------------------------------------
# 1. Geminiモデル検出 & 3分割安全生成
# --------------------------------------------------
def get_working_models():
    candidates = []
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            if "flash" in name and not any(x in name for x in ["1.5", "2.0", "2.5", "vision", "audio", "tts", "8b", "lite"]):
                candidates.append(name)
    except Exception:
        pass
    for fb in ["gemini-3.8-flash", "gemini-3.0-flash", "gemini-3-flash", "gemini-2.5-flash"]:
        if fb not in candidates:
            candidates.append(fb)
    return candidates

def call_gemini_safe(prompt, models):
    last_err = ""
    for model_name in models:
        try:
            # thinking_budget=0 が使えるモデルは思考トークンによる出力枠圧迫を防止
            try:
                cfg = types.GenerateContentConfig(
                    max_output_tokens=8192,
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                )
                res = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
            except Exception:
                cfg = types.GenerateContentConfig(max_output_tokens=8192)
                res = client.models.generate_content(model=model_name, contents=prompt, config=cfg)

            if res and res.text:
                return sanitize_html_fragment(res.text)
        except Exception as e:
            last_err = str(e)
            continue
    return f"<p>要約エラー: {last_err}</p>"

def fetch_single_feed(name, url, limit):
    results = []
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, headers=HEADERS, timeout=8)
        f = feedparser.parse(resp.content)
        for e in f.entries[:limit]:
            title = e.get("title", "").strip()
            if not title:
                continue
            raw_html = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
            clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)
            if not is_valid_article(title, clean_text):
                continue
            results.append((name, title, clean_text[:600], e.get("link", "")))
    except Exception as e:
        print(f"RSSスキップ ({name}): {e}", flush=True)
    return results

def get_all_uk_news():
    print("2. RSSフィードを並列取得中...", flush=True)
    arsenal_feeds = [
        ("Arseblog", build_url(P_HTTPS, D_ARSEBLOG, "/feed/"), 6),
        ("Football.London", build_url(P_HTTPS, D_FLONDON, "/arsenal-fc/?service=rss"), 5),
        ("Guardian", build_url(P_HTTPS, D_GUARDIAN, "/football/arsenal/rss"), 5),
        ("BBC", build_url(P_HTTPS, D_BBC, "/sport/football/teams/arsenal/rss.xml"), 4),
        ("JustArsenal", build_url(P_HTTPS, D_JUSTARS, "/feed"), 4),
        ("Arsenal Youth (U21/U18)", build_url(P_HTTPS, D_YOUTH, "/feed/"), 6)
    ]

    pl_feeds = [
        ("Guardian PL", build_url(P_HTTPS, D_GUARDIAN, "/football/premierleague/rss"), 25),
        ("BBC PL", build_url(P_HTTPS, D_BBC, "/sport/football/premier-league/rss.xml"), 25),
        ("BBC Football", build_url(P_HTTPS, D_BBC, "/sport/football/rss.xml"), 20),
        ("BBC Gossip", build_url(P_HTTPS, D_BBC, "/sport/football/gossip/rss.xml"), 2)
    ]

    seen_titles = set()
    ars_top_bucket = []
    ars_u21_bucket = []
    big6_bucket = []
    other14_bucket = []
    gossip_bucket = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_map = {}
        for name, url, limit in arsenal_feeds:
            future_map[executor.submit(fetch_single_feed, name, url, limit)] = ("ARS", name)
        for name, url, limit in pl_feeds:
            future_map[executor.submit(fetch_single_feed, name, url, limit)] = ("PL", name)

        for fut in as_completed(future_map):
            group, _ = future_map[fut]
            for name, title, text, link in fut.result():
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                entry_str = f"[{name}]\nTitle: {title}\nContent: {text}\nLink: {link}\n"

                if group == "ARS":
                    if "Youth" in name or any(k in (title + text).lower() for k in ["u21", "u18", "under-21", "under-18", "academy", "hale end", "pl2"]):
                        ars_u21_bucket.append(entry_str)
                    else:
                        if len(ars_top_bucket) < 12:
                            ars_top_bucket.append(entry_str)
                elif "Gossip" in name:
                    gossip_bucket.append(entry_str)
                else:
                    if "arsenal" in title.lower() and not is_big6_article(title, text):
                        continue
                    if is_big6_article(title, text):
                        if len(big6_bucket) < 10:
                            big6_bucket.append(entry_str)
                    else:
                        if len(other14_bucket) < 10:
                            other14_bucket.append(entry_str)

    models = get_working_models()

    # バッチ1：アーセナル（トップ 9〜11件 ＋ U21・アカデミー 4〜5件）
    prompt_1 = f"""
以下の英語記事から、①アーセナル男子トップチーム（9〜11件）と ②アーセナルU21/U18/アカデミー/ローン選手（4〜5件）を日本語で濃密に要約し、純粋なHTMLタグのみで出力してください。
※各記事は必ず `</details>` まで閉じてください。

<h2>🔴 アーセナル・トップチーム最新ニュース</h2>
<details>
  <summary>🔴 [メディア名] [具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>背景・要点：</b>[具体的な出来事や試合・契約の状況]</li>
      <li><b>発言・コメント：</b>[監督・選手・記者の具体的な発言内容]</li>
      <li><b>戦術・今後の影響：</b>[起用法・怪我・今後の見通し]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く</a></p>
  </div>
</details>

<h2>🌱 アーセナル U21・U18・アカデミー＆若手動向</h2>
<details>
  <summary>🌱 [U21/Youth] [選手名や試合結果がわかる日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>試合結果・動向：</b>[U21/U18のスコア、契約、ローン情報]</li>
      <li><b>注目選手の評価：</b>[得点者や出場メンバー、プレー内容]</li>
      <li><b>今後の展望：</b>[トップチーム帯同や昇格への期待]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く</a></p>
  </div>
</details>

【トップチーム候補】:
{chr(10).join(ars_top_bucket)}

【U21/アカデミー候補】:
{chr(10).join(ars_u21_bucket)}
"""

    # バッチ2：ビッグ6（8〜9件）と その他プレミア14クラブ（8〜9件）を1:1の同数＋移籍ゴシップ
    prompt_2 = f"""
以下の英語記事から、**①ビッグ6（8〜9件）** と **②その他プレミア14クラブ（8〜9件）** を **完全に1:1の同数** で出力し、最後に **③移籍ゴシップ（5〜6件）** を純粋なHTMLタグのみで出力してください。
※ビッグ6だけでなく、必ずその他14クラブも同じ件数出力してください。各記事は必ず `</details>` まで閉じてください。

<h2>⚔️ ビッグ6動向（シティ/リヴァプール/チェルシー/Utd/スパーズ）</h2>
<details>
  <summary>⚔️ 【クラブ名】[具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>背景・要点：</b>[具体的な出来事や背景]</li>
      <li><b>発言・コメント：</b>[監督や選手の具体的な発言]</li>
      <li><b>チーム事情・今後：</b>[戦術・怪我人・順位争いへの影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く</a></p>
  </div>
</details>

<h2>🦁 その他プレミア14クラブ動向（ビッグ6と同数）</h2>
<details>
  <summary>🦁 【クラブ名】[具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>背景・要点：</b>[具体的な出来事や背景]</li>
      <li><b>発言・詳細：</b>[監督や選手の発言・スタッツ]</li>
      <li><b>今後の影響：</b>[次節やシーズンへの影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く</a></p>
  </div>
</details>

<h2>🔄 移籍の噂・ゴシップまとめ</h2>
<details>
  <summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>
  <div class="detail-box">
    <ul>
      <li><b>移籍情報詳細：</b>[移籍金・契約条件・競合クラブなどの詳細]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く</a></p>
  </div>
</details>

【ビッグ6候補】:
{chr(10).join(big6_bucket)}

【その他14クラブ候補】:
{chr(10).join(other14_bucket)}

【移籍ゴシップ候補】:
{chr(10).join(gossip_bucket)}
"""

    print("3. Gemini翻訳実行中 (1/2: アーセナル＆U21)...", flush=True)
    html_1 = call_gemini_safe(prompt_1, models)
    print("4. Gemini翻訳実行中 (2/2: BIG6＆他14クラブ)...", flush=True)
    html_2 = call_gemini_safe(prompt_2, models)
    return html_1 + "\n" + html_2

# --------------------------------------------------
# 2. 次回日程 ＆ 前回試合スタッツ ＆ PL順位表（FPL公式データを完全統合）
# --------------------------------------------------
def get_all_football_data():
    print("5. 公式データ（日程・スタッツ・順位表・ランキング）取得中...", flush=True)
    jst = pytz.timezone("Asia/Tokyo")
    next_html = "<div class='next-box'>次回試合予定データ取得中</div>"
    stats_lines = []
    standings_lines = []

    # --- 1) 100%確実に繋がるプレミアリーグ公式 (FPL) API から基礎データを全取得 ---
    fpl_teams = {}
    fpl_short = {}
    fpl_elements = {}
    fpl_fixes = []
    ars_id = 1

    try:
        boot = requests.get(build_url(P_HTTPS, D_FPL, "/api/bootstrap-static/"), headers=HEADERS, timeout=10).json()
        for t in boot.get("teams", []):
            fpl_teams[t["id"]] = t["name"]
            fpl_short[t["id"]] = t["short_name"]
            if t["name"] == "Arsenal":
                ars_id = t["id"]
        for el in boot.get("elements", []):
            fpl_elements[el["id"]] = el

        fpl_fixes = requests.get(build_url(P_HTTPS, D_FPL, "/api/fixtures/"), headers=HEADERS, timeout=10).json()
    except Exception as e:
        print(f"FPL base err: {e}", flush=True)

    # 次回日程の確定
    ars_fixes = [f for f in fpl_fixes if f.get("team_h") == ars_id or f.get("team_a") == ars_id]
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

    # --- 2) 前回試合スタッツの構築（FotMob -> FPL公式ライブスタッツ＆ESPN） ---
    fpl_fin = [f for f in ars_fixes if f.get("finished") or f.get("finished_provisional") or (f.get("team_h_score") is not None and f.get("started"))]
    if fpl_fin:
        last_fix = fpl_fin[-1]
        gw = last_fix.get("event")
        fix_id = last_fix.get("id")
        h_id_fpl = last_fix.get("team_h")
        a_id_fpl = last_fix.get("team_a")
        h_name = fpl_teams.get(h_id_fpl, "Home")
        a_name = fpl_teams.get(a_id_fpl, "Away")
        h_score = last_fix.get("team_h_score", 0)
        a_score = last_fix.get("team_a_score", 0)
        kick_str = ""
        if last_fix.get("kickoff_time"):
            k_dt = datetime.fromisoformat(last_fix["kickoff_time"].replace("Z", "+00:00")).astimezone(jst)
            kick_str = k_dt.strftime("%Y/%m/%d")

        stats_lines.append(f"### ⚽ {h_name} {h_score} - {a_score} {a_name} (PL 第{gw}節 {kick_str})\n")

        # A. 試合の得点・アシスト・カード・セーブ内訳 (FPL fixtures stats)
        fix_stats = last_fix.get("stats", [])
        stat_label_jp = {
            "goals_scored": "⚽ ゴール",
            "assists": "🎯 アシスト",
            "own_goals": "💥 オウンゴール",
            "yellow_cards": "🟨 イエローカード",
            "red_cards": "🟥 レッドカード",
            "saves": "🧤 GKセーブ数",
            "bonus": "👑 公式MOMボーナス(3-2-1pt)"
        }
        event_rows = []
        for st in fix_stats:
            ident = st.get("identifier")
            if ident in stat_label_jp:
                h_items = [f"{fpl_elements.get(x['element'], {}).get('web_name', '')}({x['value']})" for x in st.get("h", [])]
                a_items = [f"{fpl_elements.get(x['element'], {}).get('web_name', '')}({x['value']})" for x in st.get("a", [])]
                if h_items or a_items:
                    event_rows.append(f"| **{stat_label_jp[ident]}** | {', '.join(h_items) or '-'} | {', '.join(a_items) or '-'} |")

        if event_rows:
            stats_lines.append("#### 🎯 試合サマリー＆主要イベント")
            stats_lines.append(f"| 項目 | {h_name} | {a_name} |")
            stats_lines.append("| :--- | :--- | :--- |")
            stats_lines.extend(event_rows)
            stats_lines.append("")

        # B. FPL公式ライブAPI (`/api/event/{gw}/live/`) から、この試合の全出場選手のxG・守備・BPS採点を取得
        try:
            live_url = build_url(P_HTTPS, D_FPL, f"/api/event/{gw}/live/")
            live_res = requests.get(live_url, headers=HEADERS, timeout=10).json()
            ars_match_players = []
            h_totals = {"xg": 0.0, "xa": 0.0, "tackles": 0, "cbi": 0, "recoveries": 0, "bps": 0}
            a_totals = {"xg": 0.0, "xa": 0.0, "tackles": 0, "cbi": 0, "recoveries": 0, "bps": 0}

            for el_live in live_res.get("elements", []):
                el_id = el_live.get("id")
                p_info = fpl_elements.get(el_id, {})
                t_id = p_info.get("team")
                if t_id not in (h_id_fpl, a_id_fpl):
                    continue

                # この試合（fix_id）のスタッツを抽出
                explain = el_live.get("explain", [])
                match_mins = 0
                for exp in explain:
                    if exp.get("fixture") == fix_id:
                        for s_item in exp.get("stats", []):
                            if s_item.get("identifier") == "minutes":
                                match_mins = s_item.get("value", 0)
                st = el_live.get("stats", {})
                if match_mins == 0 and st.get("minutes", 0) == 0:
                    continue

                xg_v = float(st.get("expected_goals", 0) or 0)
                xa_v = float(st.get("expected_assists", 0) or 0)
                tk_v = int(st.get("tackles", 0) or 0)
                cbi_v = int(st.get("clearances_blocks_interceptions", 0) or 0)
                rec_v = int(st.get("recoveries", 0) or 0)
                bps_v = int(st.get("bps", 0) or 0)

                target_tot = h_totals if t_id == h_id_fpl else a_totals
                target_tot["xg"] += xg_v
                target_tot["xa"] += xa_v
                target_tot["tackles"] += tk_v
                target_tot["cbi"] += cbi_v
                target_tot["recoveries"] += rec_v

                if t_id == ars_id:
                    ars_match_players.append({
                        "name": p_info.get("web_name", ""),
                        "mins": st.get("minutes", match_mins),
                        "g": st.get("goals_scored", 0),
                        "a": st.get("assists", 0),
                        "xg": xg_v,
                        "xa": xa_v,
                        "tk": tk_v,
                        "cbi": cbi_v,
                        "rec": rec_v,
                        "bps": bps_v,
                        "ict": float(st.get("ict_index", 0) or 0)
                    })

            # チーム比較スタッツ表
            stats_lines.append("#### 📊 チームスタッツ比較 (PL公式トラッキング集計)")
            stats_lines.append(f"| 指標 | {h_name} | {a_name} |")
            stats_lines.append("| :--- | :---: | :---: |")
            stats_lines.append(f"| **ゴール期待値 (xG)** | **{h_totals['xg']:.2f}** | **{a_totals['xg']:.2f}** |")
            stats_lines.append(f"| **アシスト期待値 (xA)** | {h_totals['xa']:.2f} | {a_totals['xa']:.2f} |")
            stats_lines.append(f"| **タックル成功数** | {h_totals['tackles']} | {a_totals['tackles']} |")
            stats_lines.append(f"| **クリア・ブロック・インターセプト** | {h_totals['cbi']} | {a_totals['cbi']} |")
            stats_lines.append(f"| **ボール回収数 (Recoveries)** | {h_totals['recoveries']} | {a_totals['recoveries']} |")
            stats_lines.append("")

            # この試合のアーセナル個人ランキング TOP 3
            if len(ars_match_players) >= 3:
                top_bps = sorted(ars_match_players, key=lambda x: x["bps"], reverse=True)[:3]
                top_def = sorted(ars_match_players, key=lambda x: x["tk"] + x["rec"] + x["cbi"], reverse=True)[:3]
                top_xg = sorted(ars_match_players, key=lambda x: x["xg"] + x["xa"], reverse=True)[:3]

                stats_lines.append("#### 👑 この試合のアーセナル個人ランキング")
                stats_lines.append("| 部門 | 🥇 1位 | 🥈 2位 | 🥉 3位 |")
                stats_lines.append("| :--- | :--- | :--- | :--- |")
                stats_lines.append(f"| **公式BPS採点 (総合貢献)** | **{top_bps[0]['name']}** ({top_bps[0]['bps']}pt) | {top_bps[1]['name']} ({top_bps[1]['bps']}pt) | {top_bps[2]['name']} ({top_bps[2]['bps']}pt) |")
                stats_lines.append(f"| **守備＆デュエル回収王** | **{top_def[0]['name']}** ({top_def[0]['tk']+top_def[0]['rec']+top_def[0]['cbi']}回) | {top_def[1]['name']} ({top_def[1]['tk']+top_def[1]['rec']+top_def[1]['cbi']}回) | {top_def[2]['name']} ({top_def[2]['tk']+top_def[2]['rec']+top_def[2]['cbi']}回) |")
                stats_lines.append(f"| **攻撃関与期待値 (xG+xA)** | **{top_xg[0]['name']}** ({top_xg[0]['xg']+top_xg[0]['xa']:.2f}) | {top_xg[1]['name']} ({top_xg[1]['xg']+top_xg[1]['xa']:.2f}) | {top_xg[2]['name']} ({top_xg[2]['xg']+top_xg[2]['xa']:.2f}) |")
                stats_lines.append("")

                # 全出場選手スタッツ一覧
                ars_match_players.sort(key=lambda x: (x["mins"], x["bps"]), reverse=True)
                stats_lines.append("#### 👥 アーセナル出場選手 個人スタッツ＆採点一覧")
                stats_lines.append("| 選手 | 時間 | G/A | xG | xA | タックル | ボール回収 | 守備CBI | BPS採点 |")
                stats_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for p in ars_match_players:
                    ga = f"**{p['g']}G {p['a']}A**" if (p["g"] or p["a"]) else "-"
                    stats_lines.append(f"| **{p['name']}** | {p['mins']}' | {ga} | {p['xg']:.2f} | {p['xa']:.2f} | {p['tk']} | {p['rec']} | {p['cbi']} | **{p['bps']}** |")
                stats_lines.append("")
        except Exception as e:
            print(f"FPL live match err: {e}", flush=True)

    # --- 3) 順位表＆シーズン個人ランキングの構築（100%表示保証） ---
    if fpl_elements:
        all_els = list(fpl_elements.values())
        ars_season = [p for p in all_els if p.get("team") == ars_id and p.get("minutes", 0) > 0]
        if ars_season:
            standings_lines.append("### 🔴 アーセナル シーズン個人ランキング (PL公式)\n")
            standings_lines.append("| 選手 | 時間 | G | A | xG | xA | タックル | ボール回収 | 守備CBI |")
            standings_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
            ars_season.sort(key=lambda x: (x.get("goals_scored", 0) + x.get("assists", 0), x.get("minutes", 0)), reverse=True)
            for p in ars_season[:18]:
                standings_lines.append(
                    f"| **{p.get('web_name')}** | {p.get('minutes')}' | **{p.get('goals_scored')}** | **{p.get('assists')}** | "
                    f"{p.get('expected_goals', '0.0')} | {p.get('expected_assists', '0.0')} | "
                    f"{p.get('tackles', 0)} | {p.get('recoveries', 0)} | {p.get('clearances_blocks_interceptions', 0)} |"
                )
            standings_lines.append("")

        top_g = sorted(all_els, key=lambda x: (x.get("goals_scored", 0), -x.get("minutes", 0)), reverse=True)[:5]
        top_a = sorted(all_els, key=lambda x: (x.get("assists", 0), -x.get("minutes", 0)), reverse=True)[:5]
        top_tk = sorted(all_els, key=lambda x: (x.get("tackles", 0), -x.get("minutes", 0)), reverse=True)[:5]

        standings_lines.append("### 👑 プレミアリーグ全体 個人ランキング TOP 5\n")
        standings_lines.append("| 順位 | ⚽ 得点ランキング | 🎯 アシストランキング | 🛡️ タックル奪取王 |")
        standings_lines.append("| :---: | :--- | :--- | :--- |")
        for i in range(5):
            pg, pa, pt = top_g[i], top_a[i], top_tk[i]
            standings_lines.append(
                f"| {i+1}位 | **{pg['web_name']}** ({fpl_short.get(pg['team'], '')}) **{pg['goals_scored']}G** | "
                f"**{pa['web_name']}** ({fpl_short.get(pa['team'], '')}) **{pa['assists']}A** | "
                f"**{pt['web_name']}** ({fpl_short.get(pt['team'], '')}) **{pt.get('tackles', 0)}回** |"
            )
        standings_lines.append("")

    # プレミアリーグ全20クラブ最新順位表（FPL公式試合結果から直接集計するためエラー率0%）
    if fpl_teams and fpl_fixes:
        table = {tid: {"name": name, "gp": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0} for tid, name in fpl_teams.items()}
        for f in fpl_fixes:
            if f.get("finished") or f.get("finished_provisional") or (f.get("team_h_score") is not None and f.get("started")):
                th, ta = f.get("team_h"), f.get("team_a")
                sh, sa = f.get("team_h_score", 0), f.get("team_a_score", 0)
                if th in table and ta in table and sh is not None and sa is not None:
                    table[th]["gp"] += 1
                    table[ta]["gp"] += 1
                    table[th]["gf"] += sh
                    table[th]["ga"] += sa
                    table[ta]["gf"] += sa
                    table[ta]["ga"] += sh
                    if sh > sa:
                        table[th]["w"] += 1
                        table[th]["pts"] += 3
                        table[ta]["l"] += 1
                    elif sh < sa:
                        table[ta]["w"] += 1
                        table[ta]["pts"] += 3
                        table[th]["l"] += 1
                    else:
                        table[th]["d"] += 1
                        table[ta]["d"] += 1
                        table[th]["pts"] += 1
                        table[ta]["pts"] += 1

        sorted_teams = sorted(table.values(), key=lambda x: (x["pts"], x["gf"] - x["ga"], x["gf"]), reverse=True)
        standings_lines.append("### 🏆 プレミアリーグ 最新順位表\n")
        standings_lines.append("| 順位 | クラブ | 試 | 勝 | 分 | 負 | 得 | 失 | 得失 | 勝点 |")
        standings_lines.append("| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for idx, t in enumerate(sorted_teams, 1):
            c_name = f"🔴 **{t['name']}**" if t["name"] == "Arsenal" else t["name"]
            gd = t["gf"] - t["ga"]
            gd_str = f"+{gd}" if gd > 0 else str(gd)
            standings_lines.append(f"| {idx} | {c_name} | {t['gp']} | {t['w']} | {t['d']} | {t['l']} | {t['gf']} | {t['ga']} | {gd_str} | **{t['pts']}** |")
        standings_lines.append("")

    stats_html = markdown.markdown("\n".join(stats_lines), extensions=['tables']) if stats_lines else "<p>スタッツデータなし</p>"
    standings_html = markdown.markdown("\n".join(standings_lines), extensions=['tables']) if standings_lines else "<p>順位表データなし</p>"
    return next_html, stats_html, standings_html

def main():
    jst_now = datetime.now(pytz.timezone("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M")
    next_box_html, stats_html, standings_html = get_all_football_data()
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
  h4 {{ font-size: 0.92rem; color: #ffcc00; margin: 14px 0 4px; }}
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
    <section id="news" class="content active">{news_html}</section>
    <section id="stats" class="content">{stats_html}</section>
    <section id="table" class="content">{standings_html}</section>
  </div>
<script>
  const tabIds = ['news', 'stats', 'table'];
  const btnIds = ['btn-news', 'btn-stats', 'btn-table'];
  let currentTab = 0;

  function switchTab(index) {{
    currentTab = index;
    tabIds.forEach((id, i) => {{
      const el = document.getElementById(id);
      const btn = document.getElementById(btnIds[i]);
      if (el) el.classList.toggle('active', i === index);
      if (btn) btn.classList.toggle('active', i === index);
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
    print("6. 全工程完了！", flush=True)

if __name__ == "__main__":
    main()
