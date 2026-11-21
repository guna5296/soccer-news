import os
import re
import json
import time
import calendar
import requests
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from datetime import datetime, timezone
import pytz
import markdown
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

print("1. 新着専用スクリプト開始...", flush=True)

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY is missing in GitHub Secrets.")

client = genai.Client(
    api_key=api_key,
    http_options=types.HttpOptions(timeout=90000)
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
}

HISTORY_FILE = "history.json"

def build_url(*parts):
    raw = "".join(parts)
    raw = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', raw)
    return raw.replace("[", "").replace("]", "")

# ドメイン分割定義（Markdown自動リンク化防止）
P_HTTPS = "https" + "://"
D_GUARDIAN = "www.theguardian" + ".com"
D_BBC = "feeds.bbci" + ".co.uk"
D_ARSEBLOG = "arseblog" + ".news"
D_FLONDON = "www.football" + ".london"
D_YOUTH = "arsenalyouth.wordpress" + ".com"
D_JUSTARS = "www.justarsenal" + ".com"
D_DCANNON = "dailycannon" + ".com"
D_PAINARS = "paininthearsenal" + ".com"
D_SKY = "www.skysports" + ".com"
D_ESPN = "www.espn" + ".com"
D_F365 = "www.football365" + ".com"
D_FPL = "fantasy.premierleague" + ".com"
D_GTRANS = "translate.googleapis" + ".com"

WOMEN_EXCLUDE_PATTERNS = [
    r"\barsenal women\b", r"\bwsl\b", r"\bwomen's super league\b",
    r"\blionesses\b", r"\barsenal ladies\b", r"\brenee slegers\b",
    r"\bjonas eidevall\b", r"\bwomen's champions league\b", r"\buwcl\b"
]

BIG6_PATTERNS = [
    r"\bmanchester city\b", r"\bman city\b", r"\bguardiola\b", r"\bhaaland\b", r"\bmaresca\b",
    r"\bliverpool\b", r"\bslot\b", r"\bsalah\b", r"\banfield\b",
    r"\bchelsea\b", r"\bstamford bridge\b", r"\bpalmer\b",
    r"\bmanchester united\b", r"\bman utd\b", r"\bman united\b", r"\bamorim\b", r"\bold trafford\b", r"\bcarrick\b",
    r"\btottenham\b", r"\bspurs\b", r"\bpostecoglou\b"
]

# --------------------------------------------------
# 既読履歴の読み込み・保存（同じ記事の再掲載を100%ブロック）
# --------------------------------------------------
def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("urls", [])), set(data.get("titles", []))
        except Exception:
            pass
    return set(), set()

def save_history(urls_set, titles_set):
    # 最新400件分だけ保持してファイル肥大化を防止
    urls_list = list(urls_set)[-400:]
    titles_list = list(titles_set)[-400:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"urls": urls_list, "titles": titles_list}, f, ensure_ascii=False, indent=2)

def normalize_url(url):
    return url.split("?")[0].rstrip("/").lower()

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

def sanitize_html_fragment(raw_text):
    if not raw_text:
        return ""
    cleaned = raw_text.replace("```html", "").replace("```", "").strip()
    last_open = cleaned.rfind("<details")
    last_close = cleaned.rfind("</details>")
    if last_open != -1 and (last_close == -1 or last_open > last_close):
        cleaned = cleaned[:last_open].strip()
    soup = BeautifulSoup(f"<div>{cleaned}</div>", "html.parser")
    for a in soup.find_all("a"):
        a["target"] = "_blank"
        a["rel"] = "noopener noreferrer"
    return "".join(str(child) for child in soup.div.contents) if soup.div else cleaned

# --------------------------------------------------
# バックアップ翻訳（API制限時でも新着記事を即座に日本語化）
# --------------------------------------------------
def translate_text_free(text_en):
    if not text_en:
        return ""
    try:
        url = build_url(P_HTTPS, D_GTRANS, "/translate_a/single")
        params = {"client": "gtx", "sl": "en", "tl": "ja", "dt": "t", "q": text_en[:900]}
        r = requests.get(url, params=params, headers=HEADERS, timeout=6)
        if r.status_code == 200:
            data = r.json()
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
    except Exception:
        pass
    return text_en

def build_fallback_section_html(header_title, icon, items_raw):
    if not items_raw:
        return ""
    lines = [f"<h2>{header_title}</h2>"]

    def process_item(item):
        name, title_en, content_en, link, age_str = item
        combined_ja = translate_text_free(f"{title_en}\n---\n{content_en[:500]}")
        parts = combined_ja.split("---")
        title_ja = parts[0].strip() if parts else title_en
        body_ja = parts[1].strip() if len(parts) > 1 else combined_ja

        sentences = [s.strip() for s in re.split(r'[。．\n]+', body_ja) if len(s.strip()) > 8]
        if not sentences:
            sentences = [body_ja]
        li_html = "".join(f"<li>{s}。</li>" for s in sentences[:4])

        return (
            f"<details>"
            f"<summary>{icon} [{name} / {age_str}] {title_ja}</summary>"
            f"<div class='detail-box'><ul>{li_html}</ul>"
            f"<p class='ext-link'><a href='{link}' target='_blank' rel='noopener noreferrer'>🔗 元記事(英語)を開く ※通信発生</a></p>"
            f"</div></details>"
        )

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(process_item, items_raw))
    lines.extend(results)
    return "\n".join(lines)

# --------------------------------------------------
# 1. Geminiモデル取得 & 呼び出し
# --------------------------------------------------
def get_candidate_models():
    candidates = []
    try:
        for m in client.models.list():
            name = m.name.replace("models/", "")
            if any(x in name for x in ["1.5", "2.0", "2.5", "vision", "audio", "tts", "embedding", "imagen", "veo", "aqa"]):
                continue
            if "flash" in name or "gemma-3" in name:
                candidates.append(name)
    except Exception:
        pass
    for fb in ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.0-flash"]:
        if fb not in candidates:
            candidates.append(fb)
    candidates.sort(key=lambda x: (0 if "flash" in x and "lite" not in x else 1))
    return candidates[:4]

def call_gemini_smart(prompt, models):
    for model_name in models:
        try:
            print(f"Gemini試行: {model_name} ...", flush=True)
            try:
                cfg = types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                )
                res = client.models.generate_content(model=model_name, contents=prompt, config=cfg)
            except Exception as inner_e:
                if "429" in str(inner_e) or "RESOURCE_EXHAUSTED" in str(inner_e) or "404" in str(inner_e):
                    raise inner_e
                res = client.models.generate_content(model=model_name, contents=prompt)

            if res and res.text:
                print(f"Gemini成功: {model_name}", flush=True)
                return sanitize_html_fragment(res.text)
        except Exception as e:
            print(f"モデル {model_name} スキップ ({str(e)[:70]})", flush=True)
            continue
    return None

# --------------------------------------------------
# 2. RSS並列取得（鮮度48時間チェック ＋ 既読除外）
# --------------------------------------------------
def get_entry_age_hours(entry, now_ts):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            pub_ts = calendar.timegm(parsed)
            diff_h = (now_ts - pub_ts) / 3600.0
            return max(0.0, diff_h)
        except Exception:
            pass
    return 12.0  # 日時不明なフィードは12時間前扱い

def fetch_single_feed(name, url, limit):
    results = []
    now_ts = time.time()
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(4, 6))
        f = feedparser.parse(resp.content)
        for e in f.entries[:limit]:
            title = e.get("title", "").strip()
            link = e.get("link", "").strip()
            if not title or not link:
                continue
            age_h = get_entry_age_hours(e, now_ts)
            # 72時間以上前の古い記事は最初から門前払い
            if age_h > 72.0:
                continue
            raw_html = e.get("summary", "") or (e.get("content", [{}])[0].get("value", "") if e.get("content") else "")
            clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator=" ", strip=True)
            if not is_valid_article(title, clean_text):
                continue
            results.append((name, title, clean_text[:550], link, age_h))
    except Exception as e:
        print(f"RSSスキップ ({name}): {e}", flush=True)
    return results

def get_all_uk_news():
    seen_urls, seen_titles = load_history()
    print(f"2. 既読履歴 {len(seen_urls)}件を読み込み完了。新着RSSを収集中...", flush=True)

    # 更新頻度の高い現地メディアを大幅追加して毎日新着を確保
    arsenal_feeds = [
        ("Arseblog", build_url(P_HTTPS, D_ARSEBLOG, "/feed/"), 8),
        ("Football.London", build_url(P_HTTPS, D_FLONDON, "/arsenal-fc/?service=rss"), 8),
        ("Daily Cannon", build_url(P_HTTPS, D_DCANNON, "/feed/"), 8),
        ("Pain in the Arsenal", build_url(P_HTTPS, D_PAINARS, "/feed/"), 8),
        ("JustArsenal", build_url(P_HTTPS, D_JUSTARS, "/feed"), 8),
        ("Guardian", build_url(P_HTTPS, D_GUARDIAN, "/football/arsenal/rss"), 6),
        ("BBC", build_url(P_HTTPS, D_BBC, "/sport/football/teams/arsenal/rss.xml"), 6),
        ("Arsenal Youth", build_url(P_HTTPS, D_YOUTH, "/feed/"), 8)
    ]

    pl_feeds = [
        ("Sky Sports", build_url(P_HTTPS, D_SKY, "/rss/12040"), 30),
        ("Guardian PL", build_url(P_HTTPS, D_GUARDIAN, "/football/premierleague/rss"), 25),
        ("BBC PL", build_url(P_HTTPS, D_BBC, "/sport/football/premier-league/rss.xml"), 25),
        ("Football365", build_url(P_HTTPS, D_F365, "/feed"), 25),
        ("ESPN", build_url(P_HTTPS, D_ESPN, "/espn/rss/soccer/news"), 25),
        ("BBC Gossip", build_url(P_HTTPS, D_BBC, "/sport/football/gossip/rss.xml"), 2)
    ]

    raw_ars_pool = []
    raw_pl_pool = []
    raw_gossip_pool = []
    batch_seen_titles = set()

    with ThreadPoolExecutor(max_workers=12) as executor:
        future_map = {}
        for name, url, limit in arsenal_feeds:
            future_map[executor.submit(fetch_single_feed, name, url, limit)] = ("ARS", name)
        for name, url, limit in pl_feeds:
            future_map[executor.submit(fetch_single_feed, name, url, limit)] = ("PL", name)

        try:
            for fut in as_completed(future_map, timeout=14):
                group, _ = future_map[fut]
                for name, title, text, link, age_h in fut.result():
                    norm_u = normalize_url(link)
                    norm_t = title.lower().strip()
                    # 過去に一度でも載せたURL・タイトルは完全除外
                    if norm_u in seen_urls or norm_t in seen_titles or norm_t in batch_seen_titles:
                        continue
                    batch_seen_titles.add(norm_t)

                    if group == "ARS":
                        raw_ars_pool.append((name, title, text, link, age_h))
                    elif "Gossip" in name:
                        raw_gossip_pool.append((name, title, text, link, age_h))
                    else:
                        raw_pl_pool.append((name, title, text, link, age_h))
        except TimeoutError:
            print("一部RSSをスキップして進行します", flush=True)

    # 新しい記事順にソート（公開時間が新しいものを最優先）
    raw_ars_pool.sort(key=lambda x: x[4])
    raw_pl_pool.sort(key=lambda x: x[4])

    # 48時間以内の記事を優先採用し、不足時のみ72時間以内から補充
    def pick_fresh(pool, max_count, filter_fn=None):
        fresh_48 = []
        backup_72 = []
        for item in pool:
            if filter_fn and not filter_fn(item):
                continue
            age_label = f"{int(item[4])}時間前" if item[4] < 24 else f"{int(item[4]//24)}日前"
            formatted = (item[0], item[1], item[2], item[3], age_label)
            if item[4] <= 48.0:
                fresh_48.append(formatted)
            else:
                backup_72.append(formatted)
        selected = fresh_48[:max_count]
        if len(selected) < max_count:
            selected.extend(backup_72[:max_count - len(selected)])
        return selected

    is_u21 = lambda x: ("Youth" in x[0] or any(k in (x[1] + x[2]).lower() for k in ["u21", "u18", "under-21", "under-18", "academy", "hale end", "pl2"]))
    ars_u21_raw = pick_fresh(raw_ars_pool, 5, is_u21)
    ars_top_raw = pick_fresh(raw_ars_pool, 10, lambda x: not is_u21(x))

    pl_no_ars = [x for x in raw_pl_pool if not ("arsenal" in x[1].lower() and not is_big6_article(x[1], x[2]))]
    big6_raw = pick_fresh(pl_no_ars, 9, lambda x: is_big6_article(x[1], x[2]))
    other14_raw = pick_fresh(pl_no_ars, 9, lambda x: not is_big6_article(x[1], x[2]))
    gossip_raw = pick_fresh(raw_gossip_pool, 2)

    print(f"未読新着抽出完了: Top={len(ars_top_raw)}, U21={len(ars_u21_raw)}, BIG6={len(big6_raw)}, 他14={len(other14_raw)}", flush=True)

    # 今回採用した記事を履歴に追加保存（次回以降は二度と出ない）
    for bucket in [ars_top_raw, ars_u21_raw, big6_raw, other14_raw, gossip_raw]:
        for _, title, _, link, _ in bucket:
            seen_urls.add(normalize_url(link))
            seen_titles.add(title.lower().strip())
    save_history(seen_urls, seen_titles)

    if not (ars_top_raw or ars_u21_raw or big6_raw or other14_raw):
        return "<p>現在、過去48時間以内の未読新着記事はありません（次回更新時に新しい記事が入り次第表示されます）。</p>"

    def to_prompt_str(raw_list):
        return "\n".join(f"[{n} / {age}]\nTitle: {t}\nContent: {c}\nLink: {l}\n" for n, t, c, l, age in raw_list)

    models = get_candidate_models()

    prompt_1 = f"""
以下の未読新着英語記事から、①アーセナル男子トップチーム と ②アーセナルU21/U18/アカデミー/ローン選手を日本語で濃密に要約し、純粋なHTMLタグのみで出力してください。コードブロックは不要です。
※見出しの [メディア名 / 経過時間] は入力データの表記をそのまま使ってください。

<h2>🔴 アーセナル・トップチーム最新ニュース（新着のみ）</h2>
<details>
  <summary>🔴 [メディア名 / 経過時間] [具体的で情報量の多い日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>要点・背景：</b>[具体的な背景]</li>
      <li><b>発言・引用：</b>[監督・選手・番記者の具体的なコメント翻訳]</li>
      <li><b>戦術・今後の見通し：</b>[起用法、怪我の状態、次節や今後への影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>🌱 アーセナル U21・U18・アカデミー＆ローン選手動向（新着のみ）</h2>
<details>
  <summary>🌱 [U21 / 経過時間] [若手選手名や試合結果がわかる具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>試合結果・動向：</b>[U21/U18のスコア、契約、ローン情報]</li>
      <li><b>注目選手・展望：</b>[得点者や出場メンバー、トップ昇格への期待値]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

【トップチーム新着】:
{to_prompt_str(ars_top_raw)}

【U21/アカデミー新着】:
{to_prompt_str(ars_u21_raw)}
"""

    prompt_2 = f"""
以下の未読新着英語記事から、①ビッグ6 と ②その他プレミア14クラブ を1:1の同数で要約し、さらに ③移籍ゴシップ を加えて、純粋なHTMLタグのみで出力してください。コードブロックは不要です。

<h2>⚔️ ビッグ6動向（シティ/リヴァプール/チェルシー/Utd/スパーズ）</h2>
<details>
  <summary>⚔️ 【クラブ名 / 経過時間】[具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>要点・背景：</b>[具体的な出来事や試合の背景]</li>
      <li><b>発言・詳細：</b>[監督や選手のコメント翻訳]</li>
      <li><b>チーム事情・今後：</b>[怪我人、戦術、順位争いへの影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>🦁 その他プレミア14クラブ動向（ビッグ6と同数）</h2>
<details>
  <summary>🦁 【クラブ名 / 経過時間】[具体的な日本語タイトル]</summary>
  <div class="detail-box">
    <ul>
      <li><b>要点・背景：</b>[具体的な出来事や試合の背景]</li>
      <li><b>発言・詳細：</b>[監督や選手のコメント・スタッツ]</li>
      <li><b>今後の影響：</b>[次節やシーズンへの影響]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

<h2>🔄 移籍の噂・ゴシップまとめ</h2>
<details>
  <summary>🔄 【クラブ名/選手名】[移籍の噂の見出し（情報元の新聞名）]</summary>
  <div class="detail-box">
    <ul>
      <li><b>移籍情報詳細：</b>[移籍金・契約条件・競合クラブなどの詳細]</li>
    </ul>
    <p class="ext-link"><a href="[元記事URL]">🔗 元記事(英語)を開く ※通信発生</a></p>
  </div>
</details>

【ビッグ6新着】:
{to_prompt_str(big6_raw)}

【その他14クラブ新着】:
{to_prompt_str(other14_raw)}

【移籍ゴシップ新着】:
{to_prompt_str(gossip_raw)}
"""

    print("3. パート1（アーセナル＆U21新着）生成中...", flush=True)
    html_1 = call_gemini_smart(prompt_1, models)
    if not html_1:
        html_1 = (
            build_fallback_section_html("🔴 アーセナル・トップチーム最新ニュース（新着のみ）", "🔴", ars_top_raw) + "\n" +
            build_fallback_section_html("🌱 アーセナル U21・U18・アカデミー＆ローン選手動向（新着のみ）", "🌱", ars_u21_raw)
        )
    else:
        time.sleep(4)

    print("4. パート2（ビッグ6＆他14クラブ＆移籍新着）生成中...", flush=True)
    html_2 = call_gemini_smart(prompt_2, models)
    if not html_2:
        html_2 = (
            build_fallback_section_html("⚔️ ビッグ6動向（シティ/リヴァプール/チェルシー/Utd/スパーズ）", "⚔️", big6_raw) + "\n" +
            build_fallback_section_html("🦁 その他プレミア14クラブ動向（ビッグ6と同数）", "🦁", other14_raw) + "\n" +
            build_fallback_section_html("🔄 移籍の噂・ゴシップまとめ", "🔄", gossip_raw)
        )

    return html_1 + "\n" + html_2

# --------------------------------------------------
# 3. 次回日程 ＆ 前回試合スタッツ ＆ PL順位表（PL公式FPL API直結）
# --------------------------------------------------
def get_all_football_data():
    print("5. プレミアリーグ公式データ取得中...", flush=True)
    jst = pytz.timezone("Asia/Tokyo")
    next_html = "<div class='next-box'>次回試合予定データ取得中</div>"
    stats_lines = []
    standings_lines = []

    fpl_teams = {}
    fpl_short = {}
    fpl_elements = {}
    fpl_fixes = []
    ars_id = 1

    try:
        boot = requests.get(build_url(P_HTTPS, D_FPL, "/api/bootstrap-static/"), headers=HEADERS, timeout=8).json()
        for t in boot.get("teams", []):
            fpl_teams[t["id"]] = t["name"]
            fpl_short[t["id"]] = t["short_name"]
            if t["name"] == "Arsenal":
                ars_id = t["id"]
        for el in boot.get("elements", []):
            fpl_elements[el["id"]] = el

        fpl_fixes = requests.get(build_url(P_HTTPS, D_FPL, "/api/fixtures/"), headers=HEADERS, timeout=8).json()
    except Exception as e:
        print(f"FPL base err: {e}", flush=True)

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

        try:
            live_url = build_url(P_HTTPS, D_FPL, f"/api/event/{gw}/live/")
            live_res = requests.get(live_url, headers=HEADERS, timeout=8).json()
            ars_match_players = []
            h_totals = {"xg": 0.0, "xa": 0.0, "tackles": 0, "cbi": 0, "recoveries": 0}
            a_totals = {"xg": 0.0, "xa": 0.0, "tackles": 0, "cbi": 0, "recoveries": 0}

            for el_live in live_res.get("elements", []):
                el_id = el_live.get("id")
                p_info = fpl_elements.get(el_id, {})
                t_id = p_info.get("team")
                if t_id not in (h_id_fpl, a_id_fpl):
                    continue

                explain = el_live.get("explain", [])
                match_mins = 0
                for exp in explain:
                    if exp.get("fixture") == fix_id:
                        for s_item in exp.get("stats", []):
                            if s_item.get("identifier") == "minutes":
                                match_mins = s_item.get("value", 0)
                st = el_live.get("stats", {})
                if match_mins == 0:
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
                        "mins": match_mins,
                        "g": st.get("goals_scored", 0),
                        "a": st.get("assists", 0),
                        "xg": xg_v,
                        "xa": xa_v,
                        "tk": tk_v,
                        "cbi": cbi_v,
                        "rec": rec_v,
                        "bps": bps_v
                    })

            stats_lines.append("#### 📊 チームスタッツ比較 (PL公式トラッキング集計)")
            stats_lines.append(f"| 指標 | {h_name} | {a_name} |")
            stats_lines.append("| :--- | :---: | :---: |")
            stats_lines.append(f"| **ゴール期待値 (xG)** | **{h_totals['xg']:.2f}** | **{a_totals['xg']:.2f}** |")
            stats_lines.append(f"| **アシスト期待値 (xA)** | {h_totals['xa']:.2f} | {a_totals['xa']:.2f} |")
            stats_lines.append(f"| **タックル成功数** | {h_totals['tackles']} | {a_totals['tackles']} |")
            stats_lines.append(f"| **クリア・ブロック・インターセプト** | {h_totals['cbi']} | {a_totals['cbi']} |")
            stats_lines.append(f"| **ボール回収数 (Recoveries)** | {h_totals['recoveries']} | {a_totals['recoveries']} |")
            stats_lines.append("")

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

                ars_match_players.sort(key=lambda x: (x["mins"], x["bps"]), reverse=True)
                stats_lines.append("#### 👥 アーセナル出場選手 個人スタッツ＆採点一覧")
                stats_lines.append("| 選手 | 時間 | G/A | xG | xA | タックル | 回収 | 守備CBI | BPS採点 |")
                stats_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
                for p in ars_match_players:
                    ga = f"**{p['g']}G {p['a']}A**" if (p["g"] or p["a"]) else "-"
                    stats_lines.append(f"| **{p['name']}** | {p['mins']}' | {ga} | {p['xg']:.2f} | {p['xa']:.2f} | {p['tk']} | {p['rec']} | {p['cbi']} | **{p['bps']}** |")
                stats_lines.append("")
        except Exception as e:
            print(f"FPL live match err: {e}", flush=True)

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
      <div class="tab-btn active" id="btn-news" onclick="switchTab(0)">📰 新着ニュース＆U21</div>
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
