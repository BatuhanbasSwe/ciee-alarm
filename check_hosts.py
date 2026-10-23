"""
CIEE Beacon "Find a Host" takipçisi.
Giriş yapar, host listesini çeker, öncekiyle karşılaştırır.
Yeni host varsa profil sayfasını okur, Claude'a kriterlere göre puanlatır
ve sonucu Telegram'dan gönderir.

Gerekli ortam değişkenleri (GitHub Secrets):
  CIEE_EMAIL, CIEE_PASSWORD, CIEE_PARTICIPANT_ID,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
İsteğe bağlı:
  ANTHROPIC_API_KEY  (yoksa puanlama yapılmaz, sadece ilan bildirilir)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://beacon.ciee.org"
PARTICIPANT_ID = os.environ.get("CIEE_PARTICIPANT_ID", "").strip()
FIND_URL = f"{BASE}/participant/{PARTICIPANT_ID}/connect/find-a-host"
HERE = Path(__file__).parent
STATE_FILE = HERE / "seen.json"
CRITERIA_FILE = HERE / "kriterler.txt"

CLAUDE_MODEL = "claude-sonnet-5-5"
MAX_PROFILES_PER_RUN = 15          # bir seferde en fazla bu kadar yeni profil aç
ERROR_NOTIFY_EVERY_HOURS = 6       # hata bildirimi en fazla bu sıklıkta
HEARTBEAT_UTC_HOUR = 7             # günlük "çalışıyorum" mesajı (07:00 UTC = 10:00 Türkiye)


# ---------------- yardımcılar ----------------

def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Eksik ortam değişkeni: {name}")
    return value


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def telegram(text: str, button_url: str | None = None) -> None:
    payload = {
        "chat_id": env("TELEGRAM_CHAT_ID"),
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🔗 İlanı aç", "url": button_url}]]
        }
    r = requests.post(
        f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
        json=payload,
        timeout=20,
    )
    if not r.ok:
        print("Telegram hatası:", r.status_code, r.text[:200])


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"hosts": {}, "initialized": False, "last_error_notified": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def profile_url(host_id: str) -> str:
    return f"{BASE}/participant/{PARTICIPANT_ID}/host-profile/{host_id}"


# ---------------- profilden kesin bilgileri çekme ----------------

KEY_LINE_RE = re.compile(
    r"hour|wage|overtime|tip|gratuit|rent|housing|deposit|utilit|\$", re.I
)


def key_facts(profile_text: str) -> list[str]:
    """Profildeki saat/ücret/konaklama satırlarını aynen çıkarır.
    Etiket ile değer ayrı satırdaysa ("Weekly Average Minimum Hours:" / "32") birleştirir."""
    lines = [l.strip() for l in profile_text.splitlines() if l.strip()]
    facts = []
    for i, line in enumerate(lines):
        if len(line) > 160 or not KEY_LINE_RE.search(line):
            continue
        if line.endswith(":") and i + 1 < len(lines):
            line = f"{line} {lines[i + 1]}"
        if line not in facts:
            facts.append(line)
    return facts[:40]


def weekly_numbers(profile_text: str) -> str:
    """'Weekly Average Minimum Hours' ve 'Weekly Estimated Wage' alanlarını
    Claude'a bırakmadan, doğrudan profilden okur (birden fazla pozisyon olabilir)."""
    flat = re.sub(r"\s+", " ", profile_text)
    hours = re.findall(r"Weekly Average Minimum Hours:?\s*([\d.,]+)", flat, re.I)
    wages = re.findall(r"Weekly Estimated Wage:?\s*\$?\s*([\d.,]+)", flat, re.I)
    parts = []
    for i in range(max(len(hours), len(wages))):
        h = f"min {hours[i]} saat/hafta" if i < len(hours) else ""
        w = f"tahmini ${wages[i]}/hafta" if i < len(wages) else ""
        parts.append(", ".join(p for p in (h, w) if p))
    return " | ".join(parts)


# ---------------- Claude puanlaması ----------------

def score_with_claude(profile_text: str) -> dict | None:
    """{"score": 7, "summary": "...", "pros": [...], "cons": [...], "wage": "..."} ya da None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    criteria = CRITERIA_FILE.read_text(encoding="utf-8") if CRITERIA_FILE.exists() else ""
    facts = "\n".join(key_facts(profile_text)) or "(bulunamadı)"
    prompt = f"""Aşağıda CIEE Work and Travel programından bir işveren (host) profili var.
Bu işi aşağıdaki kriterlerime göre değerlendir.

<kriterler>
{criteria}
</kriterler>

<profilden_kesin_bilgiler>
Bu satırlar profilden aynen alındı; saat, ücret ve konaklama için önce bunlara bak.
"Weekly Average Minimum Hours" = haftalık çalışma saati (en az),
"Weekly Estimated Wage" = haftalık tahmini kazanç.
{facts}
</profilden_kesin_bilgiler>

<host_profili>
{profile_text[:20000]}
</host_profili>

Sadece şu JSON formatında, Türkçe cevap ver (başka hiçbir şey yazma):
{{
  "score": 1-10 arası tam sayı,
  "wage": "maaş özeti, örn. $12/saat + bahşiş",
  "hours": "haftalık çalışma saati ve overtime bilgisi, örn. 40+ saat/hafta, overtime var",
  "housing": "konaklama özeti, örn. $98/hafta, işe yürüme mesafesi",
  "area": "bölgenin nasıl bir yer olduğunu anlatan 2-3 cümle",
  "summary": "tek cümlelik genel değerlendirme",
  "pros": ["en fazla 3 kısa artı"],
  "cons": ["en fazla 3 kısa eksi"]
}}
Maaş, saat ve konaklama için sadece profildeki bilgiyi kullan; yoksa "belirtilmemiş" yaz, uydurma.
Profilde haftalık saat ya da haftalık tahmini kazanç yazıyorsa "hours" alanına mutlaka onu yaz.
"area" için profildeki bilgiyle birlikte o kasaba/bölge hakkındaki genel bilgini de kullanabilirsin:
ne kadar kalabalık/turistik olduğu, yaz sezonunda nasıl olduğu, en yakın büyük şehre uzaklığı,
ikinci iş ve sosyal hayat açısından nasıl olduğu. Emin olmadığın şeyi kesin gibi yazma."""

    global LAST_CLAUDE_ERROR
    LAST_CLAUDE_ERROR = ""
    for attempt in range(4):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 3000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            # Dakikalık token sınırı (429) ya da geçici yoğunluk (529/5xx): bekle, tekrar dene
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 3:
                wait = int(r.headers.get("retry-after", "0") or 0) or 30 * (attempt + 1)
                print(f"Claude {r.status_code}, {wait} sn bekleniyor...")
                time.sleep(min(wait, 90))
                continue
            if not r.ok:
                LAST_CLAUDE_ERROR = f"API {r.status_code}: {r.text[:200]}"
                break
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                LAST_CLAUDE_ERROR = f"cevapta JSON yok (stop_reason={data.get('stop_reason')})"
                break
            return json.loads(match.group(0))
        except Exception as e:
            LAST_CLAUDE_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt < 3:
                time.sleep(10)
                continue
    print("Claude puanlama hatası:", LAST_CLAUDE_ERROR)
    return None


LAST_CLAUDE_ERROR = ""


def format_message(host_id: str, host: dict, review: dict | None, numbers: str = "") -> str:
    head = (
        f"🆕 <b>Yeni host!</b>\n"
        f'<b><a href="{profile_url(host_id)}">{esc(host["name"])}</a></b>\n📍 {esc(host["location"])}\n'
    )
    if numbers:
        head += f"📊 <b>Profilde:</b> {esc(numbers)}\n"
    link = f'\n🔗 {profile_url(host_id)}'
    if not review:
        if LAST_CLAUDE_ERROR:
            head += f"\n⚠️ Claude puanlayamadı: {esc(LAST_CLAUDE_ERROR)}\n"
        return head + link

    try:
        score = int(review.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    badge = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"
    lines = [
        head,
        f"{badge} <b>Puan: {score}/10</b>",
        f"💵 {esc(review.get('wage', '-'))}",
        f"⏱️ {esc(review.get('hours', '-'))}",
        f"🏠 {esc(review.get('housing', '-'))}",
        f"\n🗺️ {esc(review.get('area', ''))}",
        f"\n{esc(review.get('summary', ''))}",
    ]
    for p in (review.get("pros") or [])[:3]:
        lines.append(f"✅ {esc(p)}")
    for c in (review.get("cons") or [])[:3]:
        lines.append(f"❌ {esc(c)}")
    return "\n".join(lines) + "\n" + link


# ---------------- tarayıcı kısmı ----------------

def click_if_present(page, selector: str) -> None:
    try:
        loc = page.locator(selector).first
        if loc.is_visible(timeout=1500):
            loc.click()
    except Exception:
        pass


# Profil sayfasında her pozisyon ("Sales Associate ... $15/hour") kapalı bir panel.
# Saat, haftalık kazanç, bahşiş ve iş tanımı panel açılmadan görünmüyor;
# görünmeyen metin inner_text() ile okunamadığı için panelleri açıyoruz.
WAGE_HEADER_RE = re.compile(r"\$\s?\d[\d.,]*\s*/\s*h(ou)?r", re.I)

VISIBLE_HOURS_JS = """() => {
  let n = 0;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = w.currentNode;
    if (/Weekly Average Minimum Hours/i.test(t.nodeValue) &&
        t.parentElement && t.parentElement.getClientRects().length) n++;
  }
  return n;
}"""

HIDDEN_FIELDS_JS = """() => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = w.currentNode;
    if (!/Weekly Average Minimum Hours|Weekly Estimated Wage|^\\s*Tips?\\s*:/i.test(t.nodeValue)) continue;
    let el = t.parentElement;
    // Değer ayrı bir etikette olabilir ("Hours: <b>37</b>"): rakam görene kadar bir üst öğeye çık
    for (let i = 0; i < 3 && el && !/:\\s*\\S/.test(el.textContent); i++) el = el.parentElement;
    if (el) out.push(el.textContent.replace(/\\s+/g, " ").trim().slice(0, 400));
  }
  return [...new Set(out)];
}"""


def expand_job_panels(page) -> None:
    """Pozisyon panellerini açar. Zaten açık bir paneli kapattıysak geri açar."""
    try:
        headers = page.get_by_text(WAGE_HEADER_RE).element_handles()[:10]
    except Exception as e:
        print("Pozisyon başlıkları bulunamadı:", e)
        return
    for h in headers:
        try:
            if not h.is_visible():
                continue
            before = page.evaluate(VISIBLE_HOURS_JS)
            h.click(timeout=3000)
            page.wait_for_timeout(700)
            if page.evaluate(VISIBLE_HOURS_JS) < before:
                h.click(timeout=3000)
                page.wait_for_timeout(700)
        except Exception as e:
            print("Pozisyon paneli açılamadı:", e)
    print(f"  {len(headers)} pozisyon başlığı, {page.evaluate(VISIBLE_HOURS_JS)} görünür saat alanı")


def hidden_job_fields(page) -> list[str]:
    """Panel açılamasa bile DOM'da duran saat/kazanç/bahşiş alanlarını okur."""
    try:
        return page.evaluate(HIDDEN_FIELDS_JS)
    except Exception as e:
        print("Pozisyon alanları okunamadı:", e)
        return []


def scrape(known_ids: set, fetch_profiles: bool = True) -> tuple[dict, dict]:
    """
    (hosts, profiles) döndürür.
    hosts:    {host_id: {"name", "location"}}
    profiles: {host_id: profil sayfası metni}  -- sadece yeni hostlar için
    """
    email = env("CIEE_EMAIL")
    password = env("CIEE_PASSWORD")
    env("CIEE_PARTICIPANT_ID")

    with sync_playwright() as p:
        # GitHub sunucularında Google Chrome hazır kurulu; onu kullan (indirme gerekmez).
        # Bulunamazsa Playwright'ın kendi Chromium'una düş.
        try:
            browser = p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # 1) Giriş
        page.goto(f"{BASE}/login/", wait_until="networkidle", timeout=60000)
        click_if_present(page, "button:has-text('Ok')")          # bakım duyurusu
        click_if_present(page, "text=Agree and Dismiss")         # çerez bandı

        page.fill("form[name=loginForm] input[name=loginUsername]", email)
        page.fill("form[name=loginForm] input[name=loginPassword]", password)
        page.click("form[name=loginForm] button[type=submit]")

        try:
            page.wait_for_url(lambda url: "/login" not in url, timeout=45000)
        except PWTimeout:
            browser.close()
            raise RuntimeError("Giriş yapılamadı (şifre değişmiş olabilir ya da site doğrulama istiyor).")

        # 2) Find a Host listesi
        page.goto(FIND_URL, wait_until="networkidle", timeout=60000)
        click_if_present(page, "button:has-text('Ok')")
        if "/login" in page.url:
            browser.close()
            raise RuntimeError("Oturum açıldı ama Find a Host sayfası girişe geri attı.")

        try:
            page.wait_for_selector("a.host-profile", timeout=20000)
        except PWTimeout:
            pass  # liste gerçekten boş olabilir

        for _ in range(6):  # lazy-load ihtimaline karşı
            page.mouse.wheel(0, 4000)
            time.sleep(0.8)

        items = page.eval_on_selector_all(
            "a.host-profile",
            """els => els.map(a => ({
                href: a.getAttribute('href') || '',
                name: (a.querySelector('h1')?.innerText || '').trim(),
                location: (a.querySelector('h3')?.innerText || '').trim(),
            }))""",
        )
        if "SEARCH RESULTS" not in page.inner_text("body").upper():
            browser.close()
            raise RuntimeError("Find a Host sayfası beklendiği gibi yüklenmedi (site değişmiş olabilir).")

        hosts = {}
        for it in items:
            href = it["href"].split("?")[0]
            if "/host-profile/" not in href:
                continue
            hid = href.rstrip("/").split("/")[-1]
            if hid not in hosts or not hosts[hid]["name"]:
                hosts[hid] = {"name": it["name"], "location": it["location"]}

        # 3) Yeni hostların profil sayfalarını oku
        profiles = {}
        new_ids = [h for h in hosts if h not in known_ids][:MAX_PROFILES_PER_RUN] if fetch_profiles else []
        for hid in new_ids:
            try:
                page.goto(profile_url(hid), wait_until="networkidle", timeout=45000)
                page.wait_for_selector("text=AVAILABLE JOB INFORMATION", timeout=15000)
                expand_job_panels(page)
                text = page.inner_text("body")
                # Sayfanın alt bilgisini at
                text = text.split("CONTACT CIEE")[0]
                hidden = hidden_job_fields(page)
                if hidden:
                    text += "\n\nPozisyon alanları:\n" + "\n".join(hidden)
                profiles[hid] = re.sub(r"\n{3,}", "\n\n", text).strip()
            except Exception as e:
                print(f"Profil okunamadı ({hid}):", e)
            time.sleep(1.5)  # siteyi yormamak için

        browser.close()
    return hosts, profiles


# ---------------- ana akış ----------------

def main() -> None:
    state = load_state()
    previous = state.get("hosts", {})
    first_run = not state.get("initialized")

    try:
        # İlk çalıştırmada mevcut ilanları puanlamaya gerek yok
        hosts, profiles = scrape(set(previous), fetch_profiles=not first_run)
    except Exception as e:
        print("HATA:", e)
        last = state.get("last_error_notified")
        should_notify = (
            last is None
            or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            > ERROR_NOTIFY_EVERY_HOURS * 3600
        )
        if should_notify:
            telegram(f"⚠️ CIEE takipçisi çalışamadı:\n{esc(e)}")
            state["last_error_notified"] = now_iso()
            save_state(state)
        sys.exit(1)

    state["last_error_notified"] = None
    new_ids = [h for h in hosts if h not in previous]
    print(f"Toplam {len(hosts)} host, {len(new_ids)} yeni.")

    if first_run:
        lines = [f"• {esc(v['name'])} — {esc(v['location'])}" for v in hosts.values()]
        telegram(
            f"✅ CIEE takibi başladı. Şu an {len(hosts)} host var:\n"
            + "\n".join(lines)
            + "\n\nYeni host eklenince puanlayıp haber vereceğim."
        )
        state["initialized"] = True
    else:
        for hid in new_ids:
            text = profiles.get(hid, "")
            numbers = weekly_numbers(text)
            print(f"[{hid}] profil {len(text)} karakter | {numbers or 'haftalık saat/kazanç bulunamadı'}")
            global LAST_CLAUDE_ERROR
            LAST_CLAUDE_ERROR = "" if text else "profil sayfası okunamadı"
            review = score_with_claude(text) if text else None
            telegram(format_message(hid, hosts[hid], review, numbers), button_url=profile_url(hid))

    state["hosts"] = {
        hid: {**v, "first_seen": previous.get(hid, {}).get("first_seen", now_iso())}
        for hid, v in hosts.items()
    }

    # Günlük "hayattayım" mesajı: her gün Türkiye saatiyle 10:00'dan sonraki ilk çalıştırmada.
    # Bu mesaj gelmezse sistem bir yerde durmuş demektir.
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if now.hour >= HEARTBEAT_UTC_HOUR and state.get("last_heartbeat") != today and not first_run:
        telegram(
            f"🟢 Sistem çalışıyor. Şu an {len(hosts)} host var, "
            f"15 dakikada bir kontrol ediliyor.\n"
            f"Bu mesaj her sabah gelir; gelmediği gün bir sorun var demektir."
        )
        state["last_heartbeat"] = today

    save_state(state)


if __name__ == "__main__":
    main()
