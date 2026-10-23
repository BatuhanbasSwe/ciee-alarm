# CIEE yeni host alarmı — kurulum

Her 15 dakikada bir GitHub sunucularında çalışır, CIEE Beacon'a giriş yapıp
"Find a Host" listesine bakar. Yeni host eklenince Telegram'dan mesaj gelir.
Bilgisayarının açık olmasına gerek yok.

## 1. Telegram botu (5 dk)

1. Telegram'da **@BotFather**'ı aç → `/newbot` yaz → bota bir isim ve kullanıcı adı ver.
2. BotFather sana bir **token** verir (`123456:ABC...` gibi). Bunu bir yere kopyala.
3. Yeni botunu Telegram'da aç ve **Start**'a bas (bir mesaj at, mesela "selam").
4. Tarayıcıda şu adresi aç (TOKEN yerine kendi token'ını yaz):
   `https://api.telegram.org/botTOKEN/getUpdates`
5. Çıkan yazıda `"chat":{"id":123456789` kısmındaki sayı senin **chat ID**'n.

## 2. GitHub reposu

1. github.com → **New repository** → isim: `ciee-host-alarm`.
   **Public** seç (aşağıdaki "Neden public?" notuna bak). README ekleme.
2. Repoda **Add file → Upload files** ile şu dosyaları yükle:
   - `check_hosts.py`
   - `requirements.txt`
   - `kriterler.txt`
   - `.github/workflows/check.yml` (klasör yapısı önemli: önce "Create new file" ile
     ismi `.github/workflows/check.yml` yazıp içeriğini yapıştırmak en kolayı)
3. **Settings → Actions → General** → en altta "Workflow permissions" →
   **Read and write permissions** seç → Save.

## 3. Secrets (şifreler)

**Settings → Secrets and variables → Actions → New repository secret** ile
şunları tek tek ekle:

| Name | Value |
|---|---|
| `CIEE_EMAIL` | CIEE giriş mailin |
| `CIEE_PASSWORD` | CIEE şifren |
| `CIEE_PARTICIPANT_ID` | `394999` |
| `TELEGRAM_BOT_TOKEN` | BotFather'ın verdiği token |
| `TELEGRAM_CHAT_ID` | 1. adımda bulduğun sayı |

Puanlama için bir tane daha:

| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys → Create Key |

Bu anahtar claude.ai aboneliğinden ayrı, kullandıkça ödenir. Console'da
**Billing** kısmından az bir kredi (ör. $5) yüklemen yeterli; ilan başına
maliyet kuruşlar düzeyinde. Bu secret'ı eklemezsen sistem yine çalışır,
sadece puan vermeden ilanı bildirir.

Secrets şifreli saklanır; repo public olsa bile kimse göremez, loglarda da `***` görünür.

## 4. İlk çalıştırma

**Actions** sekmesi → soldan "CIEE host kontrolü" → **Run workflow**.
1–2 dakika sonra Telegram'a "✅ CIEE takibi başladı. Şu an X host var…" mesajı gelmeli.
Ondan sonra her 15 dakikada bir kendiliğinden çalışır, sadece yeni host olunca yazar.

## Mesajlar

- 🆕 **Yeni host!** — isim, konum, Claude'un 10 üzerinden puanı (🟢 8+, 🟡 5–7, 🔴 4 ve altı),
  maaş ve konaklama özeti, artılar/eksiler ve profil linki.

## Kriterleri değiştirmek

Puanlama `kriterler.txt` dosyasına göre yapılır. GitHub'da dosyayı açıp kalem
ikonuna bas, düz Türkçe ile istediğin gibi değiştir ve kaydet. Bir sonraki
çalıştırmadan itibaren yeni kriterlerle puanlar.
- ⚠️ **Takipçi çalışamadı** — giriş başarısız, site değişmiş vs. En fazla 6 saatte bir gelir.
  CIEE şifreni değiştirirsen `CIEE_PASSWORD` secret'ını da güncelle.

## Notlar

- **Neden public?** Private repolarda GitHub Actions ayda 2000 dakika ile sınırlı;
  15 dakikada bir çalışmak bunu aşar. Public repoda sınır yok. Repoda sadece kod
  ve host listesi (`seen.json`) görünür, şifrelerin görünmez.
  Private istersen `check.yml`'deki `*/15` değerini `*/30` yap.
- GitHub zamanlamayı bazen 5–15 dk geciktirebilir; bu normal.
- Durdurmak için: **Actions → CIEE host kontrolü → ⋯ → Disable workflow**.
- CIEE'nin bakım saatlerinde hata mesajı gelebilir; kendiliğinden düzelir.
