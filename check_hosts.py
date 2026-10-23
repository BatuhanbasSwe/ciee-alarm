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


# ---------------- Claude puanlaması ----------------

def score_with_claude(profile_text: str) -> dict | None:
    """{"score": 7, "summary": "...", "pros": [...], "cons": [...], "wage": "..."} ya da None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    criteria = CRITERIA_FILE.read_text(encoding="utf-8") if CRITERIA_FILE.exists() else ""
    prompt = f"""Aşağıda CIEE Work and Travel programından bir işveren (host) profili var.
Bu işi aşağıdaki kriterlerime göre değerlendir.

<kriterler>
{criteria}
</kriterler>

<host_profili>
{profile_text[:12000]}
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
"area" için profildeki bilgiyle birlikte o kasaba/bölge hakkındaki genel bilgini de kullanabilirsin:
ne kadar kalabalık/turistik olduğu, yaz sezonunda nasıl olduğu, en yakın büyük şehre uzaklığı,
ikinci iş ve sosyal hayat açısından nasıl olduğu. Emin olmadığın şeyi kesin gibi yazma."""

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
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        match = re.search(r"\{.*\}", text, re.S)
        return json.loads(match.group(0)) if match else None
    except Exception as e:
        print("Claude puanlama hatası:", e)
        return None


def format_message(host_id: str, host: dict, review: dict | None) -> str:
    head = (
        f"🆕 <b>Yeni host!</b>\n"
        f'<b><a href="{profile_url(host_id)}">{esc(host["name"])}</a></b>\n📍 {esc(host["location"])}\n'
    )
    link = f'\n🔗 {profile_url(host_id)}'
    if not review:
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
                text = page.inner_text("body")
                # Sayfanın alt bilgisini at
                text = text.split("CONTACT CIEE")[0]
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
            review = score_with_claude(profiles[hid]) if hid in profiles else None
            telegram(format_message(hid, hosts[hid], review), button_url=profile_url(hid))

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
