# Tradr

Arcus ve **Lighter Robinhood Chain** üzerinde aynı ekonomik maruziyeti long/short bacaklarıyla eşleştiren terminal botu. Windows ve Linux, Python 3.12. Varsayılan **paper** modu gerçek emir göndermez.

Bot yalnızca fiyat farkının dört emrin maliyetini, kayma rezervini ve olumsuz fonlama rezervini karşılaması halinde giriş dener. Kayıpsızlık, yalnızca fee kaybı, puan kazanımı veya likidasyonsuz çalışma garantisi yoktur. Platformlar arasında atomik işlem veya ortak teminat yoktur.

## Hızlı başlangıç — Windows

```powershell
git clone https://github.com/FatihX1X/Tradr.git
Set-Location Tradr
git switch codex/tradr-hedge-bot
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-lock.txt -e ".[dev]"
.\.venv\Scripts\tradr.exe setup
.\.venv\Scripts\tradr.exe doctor --signer-check
.\.venv\Scripts\tradr.exe run --mode paper --seconds 60
```

`setup` günlük kayıp durdurma sınırını USD olarak sorar ve **config.local.json** oluşturur. `.venv` içindeki Python da kullanılabilir: `python -m tradr`. Python komutu yüklü değilse sistemin Python 3.12 kurulumunu kullanın; proje bir Python dağıtımı içermez.

Linux kurulumu:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -c requirements-lock.txt -e '.[dev]'
.venv/bin/tradr setup --daily-loss-limit 1
.venv/bin/tradr run --mode paper --seconds 60
```

Bu örnekteki `1` USD kullanıcı seçiminin örneğidir; sunucuda limit verilmediğinde canlı mod başlamaz. Paper modu yeterli net fırsat yoksa hiç işlem açmayabilir.

## Hesaplar ve canlı çalışma

Önce platform arayüzlerinden hesapları fonlayın ve **özel API anahtarları** oluşturun. Bot seed phrase veya ana Ethereum cüzdanının private key'ini kullanmaz; API anahtarını kaydetmez ve para yatırma/çekme/bridge işlemi yapmaz. Bu sürüm cüzdanla API anahtarı kayıt işlemini otomatikleştirmez.

`config.local.json` alanları:

| Alan | Değer / anlam |
|---|---|
| `arcus_address` | Arcus API anahtarının kayıtlı olduğu `0x` EVM adresi |
| `arcus_account_index` | Yalnızca bot için kullanılan Arcus subaccount, 0–9 |
| `lighter_account_index` | **RH ortamındaki** pozitif integer account index |
| `lighter_api_key_index` | RH'de 4–156 veya 158–254; 0–3 ve 157 kullanılmaz |
| `daily_loss_limit_usd` | Kullanıcının seçtiği pozitif USD durdurma eşiği, zorunlu |
| `paper_capital_per_venue` | Simülasyonda platform başına `100` USD |
| `max_leg_usd` | Bacak başına en çok `50` USD |
| `equity_fraction` | Küçük hesabın equity'sinin en çok `0.5` katı |
| `profiles_path` / `boosts_path` | İncelenmiş sözleşme ve kampanya kayıtları |
| `health_port` | Varsayılan `8787`; kapatmak için `0` |

Emirler USDG teminatlı RH instance'a gönderilir. Lighter Core hesabı, key'i veya nonce'u RH'de geçerli değildir. Platform URL ve imzalama chain ID'si kodda birlikte sabittir: `https://api.rh.lighter.xyz`, **466324**. Arcus: `https://api.arcus.xyz`.

Anahtarları kendi terminalinizde geçmişe gerçek değer yazmadan yükleyin:

```powershell
$env:ARCUS_API_PRIVATE_KEY = [Net.NetworkCredential]::new('', (Read-Host 'Arcus Ed25519 API private key' -AsSecureString)).Password
$env:LIGHTER_API_PRIVATE_KEY = [Net.NetworkCredential]::new('', (Read-Host 'Lighter RH API private key' -AsSecureString)).Password
.\.venv\Scripts\tradr.exe doctor --mode live
.\.venv\Scripts\tradr.exe run --mode live
```

Arcus anahtarı 32 bayt Ed25519 seed'in hex gösterimi veya Ed25519 PEM içeriğidir. Lighter anahtarı resmi SDK'nın API signing private key'idir. **Bu değerleri chate, issue'ya veya Git'e eklemeyin.** CLI `.env` dosyasını kendiliğinden yüklemez; Docker Compose `.env` kullanabilir. Linux'ta secret manager veya aşağıdaki systemd environment dosyası kullanılabilir.

`doctor --mode live` Arcus anahtarının kayıt, subaccount kapsamı ve süresini doğrular; RH auth token'ıyla hesabın gerçek fee tick değerlerini, mevcut pozisyonları ve emirleri okur. Emir veya leverage değişikliği göndermez. Anahtarsız `doctor --signer-check`, geçici test anahtarıyla yerel native signing'i doğrular. Canlı emirler hesap bilgileri ve gerçek kimliklerle bu teslim sırasında denenmemiştir; read-only hesap doğrulamasını ardından küçük, gözetimli ilk çalıştırmayı uygulayın.

Hesap/subaccount yalnızca bu bot tarafından kullanılmalıdır. Aynı hesabı, API anahtarını veya başka subaccount'lardaki aynı cüzdan emirlerini paralel elle yönetmeyin. Başlangıçtaki yabancı pozisyon ve emirler botu durdurur. Çalışırken teminat transferi yapmayın: Arcus net deposits bilgisi vardır, RH toplam sermaye akışı için aynı veri yoktur; bu durumda günlük ölçüm birleşik equity düşüşünü kullanır ve yeni yatırılan para zararı maskeleyebilir.

## Ortak tüm piyasalar ve sözleşme incelemesi

Her iki ortamda listelenen kripto, hisse, ETF/endeks ve emtia perpetual'ları keşfedilir. Spot borçlanma/short bulunmaz. Sembolleri eşleşen piyasalar **otomatik olarak hedge uyumlu kabul edilmez**. Farklı sembol alias'ı yalnızca açık sözleşme profiliyle kullanılabilir; örneğin bir altın ETF'si kendiliğinden XAU'ya eşleştirilmez.

`profiles.json` bu teslim sırasında keşfedilen ortak piyasa kayıtlarını içerir. Standart kripto kayıtları için incelenmiş lineer USD/native-base miktar profilleri vardır. Hisse/ETF/emtia kayıtları, iki tarafın temettü/split/roll ve olay takvimi eksiksiz doğrulanamadığından **approved=false** gelir. Bu piyasalara ilişkin yürütme desteği vardır; eksik ekonomik bilgiyi tahmin ederek açılmazlar. `doctor` her piyasanın engellenme nedenini gösterir.

Yeni/alias piyasalar için onaysız şablon alın:

```powershell
.\.venv\Scripts\tradr.exe doctor --export-profiles profiles.review.json
```

Şablonları mevcut `profiles.json` ile birleştirirken şu bilgileri resmi kaynaklarla inceleyin:

- `underlying`, USD fiyat birimi, `linear-perpetual` settlement; native kontrat miktarının underlying birimine çarpanı (`multiplier`). Mevcut onaylı kayıtların çarpanı 1'dir. Birim başına underlying fiyatı olmayan ürün bu profile uygun değildir.
- Her iki platform için uyumlu `oracle`, `dividends`, `splits`, `roll` politikaları ve `margin_mode`. Farklı sağlayıcılı underlying endekslerde canlı oracle farkı ayrıca 50 baz puanla sınırlanır; eşit profile adı fiyat eşitliği garantisi değildir.
- `sources`, `reviewed_at`, `valid_until`: iki venue'nun resmi kaynakları ve timezone içeren inceleme/geçerlilik zamanları. Tarihi yalnızca gerçek yeniden inceleme sonrası ilerletin.
- RWA `event_state`: session/holiday/corporate action kontrolü, kaynakları, `checked_at`/`valid_until` ve önümüzdeki taşıma penceresinde olay bulunmadığına ilişkin doğrulama. Olay doğrulanamıyorsa giriş açılmaz. Snapshot kalan azami dört saatlik pencereyi kapsamalıdır.
- Seans dışı giriş için `offhours_approved=true`, bilinen seans ve güncel Arcus fiyat bandı gerekir. Canlı mark/oracle ve defter de iki saniyeden eski olamaz. Native isolated ürünler 1x margin ile hazırlanır; doğrulanmayan mode/teminat bilgisi işlemi durdurur.

Kayıtlar yedi günlük inceleme süresiyle gelir; süresi geçen kayıtlar durur. Fiyatlama ve olay uyumu doğrulanamayan ürünler, boost olsa bile açılmaz.

## Boost ve emir yöntemi

`boosts.json` resmi kaynak, erişim yolu, platform, sembol, stil, çarpan ve geçerlilik kaydını tutar. API'de uygunluğu ayrıca doğrulanmış, güncel kayıtlar yalnızca maliyet/risk kontrollerini geçen fırsatları öne alır. Kaynak linkini eklemek tek başına uygunluk kanıtı değildir; `evidence` gerçek koşulları açıklamalıdır.

Teslim edilen Robinhood Wallet 2x kaydı bilgi amaçlıdır: `access_path=robinhood_wallet`, API uygunluğu doğrulanmamış. Bot bunu **2x API boost'u olarak kullanmaz**, başka integrator'a ait attribution taklit etmez. API için doğrulanmış parite veya maker boost'u bu teslim sırasında bulunmadı. Boost yoksa normal ekonomik fırsatlar taranır.

Varsayılan iki bacak IOC'dir. Gerçek bir API boost'u maker gerektirirse uygun platformda post-only, karşı tarafta IOC hedge kullanılır. Maker 30 saniye sonra iptal edilir, sunucu tarafında 60 saniyelik cancel switch kurulur. Geç gelen fills ayrıca uzlaştırılır. Cancel switch pozisyonu kapatmaz ve process/network kaybında hedge garantisi sağlamaz.

Yapay hacim, kendine işlem, puan için giriş/çıkış döngüsü veya scoring kuralını yanıltma yoktur. Lighter kuralları ağırlıklı amacı puan kasmak olan otomasyonları kapsam dışı bırakabilir; görünür puanlar kesin kazanım anlamına gelmez.

## Kontroller, durdurma ve kurtarma

```powershell
.\.venv\Scripts\tradr.exe status --mode paper
.\.venv\Scripts\tradr.exe report --mode paper
.\.venv\Scripts\tradr.exe stop --mode paper
# Canlı süreç durduktan sonra mevcut bot emirlerini/pozisyonlarını uzlaştırıp kapatmayı denemek:
.\.venv\Scripts\tradr.exe flatten --mode live
```

- Günlük limit komisyon, fonlama ve gerçekleşmemiş PnL dâhil toplam equity düşüşünü ölçer. Europe/Istanbul gününe göre SQLite'ta kalır; restart sayacı sıfırlamaz. Limit, azami kayıp garantisi değildir.
- Bacaklar bağımsız risk taşır. Likidasyon mesafesi %20 altında giriş durur, %15 altında çıkış denenir. 1x venue leverage ayarı ve düşük notional kullanılır; karşı taraftaki kâr kaybeden hesabın collateral'ına taşınmaz.
- Normal kayma üst sınırı 20, acil kapanış 100 baz puandır. Likidite yoksa sınırsız piyasa emri gönderilmez; `recovery` bildirilir. Ağ kesilmesinde platform arayüzünden müdahale gerekebilir.
- Her emir ağ gönderiminden önce journal'a yazılır. `200`/`202` veya tx hash gerçekleşme değildir. Kayıp yanıt sonrasında aynı giriş yeniden gönderilmez. Belirsiz teslim, iptal veya fill durumu yeni girişleri engeller.
- Restart eski bot pozisyonlarını uzlaştırıp kapatmayı dener ve yeni işlem açmadan çıkar. Onaylanan toparlanma sonrasında kullanıcı yeni çalışma başlatır. Journal/state dizinini pozisyonlar açıkken silmeyin veya başka hesapla yeniden kullanmayın.
- Ctrl+C/SIGTERM `stop` ister. Maker beklemesi sırasında da durdurma kontrol edilir. Health endpoint yalnızca loopback'te `http://127.0.0.1:8787/health`; değişiklik yapan HTTP endpoint'i yoktur. `status` kayıtlı zamanı gösterir; process kapalıyken bu eski bir snapshot'tır.

## Sunucu

Docker'da `config.example.json` dosyasını `config.local.json` olarak kopyalayın, `daily_loss_limit_usd` seçin ve yolları `/app/.tradr`, `/app/profiles.json`, `/app/boosts.json` veya bu dosyanın yanındaki göreli yollar olarak ayarlayın. Windows `setup` çıktısındaki mutlak Windows yollarını container'a taşımayın.

```bash
docker compose build
docker compose up tradr
# Açıkça etkinleştirilen canlı çalıştırma:
docker compose run --rm tradr run --mode live
# Container içindeki paper kontrolü:
docker compose exec tradr tradr --config /app/config.local.json stop --mode paper
```

State volume kalıcıdır. Compose otomatik yeniden başlatmaz; durdurma için 90 saniye verir. Health portu host'a açılmaz. Docker bu geliştirme ortamında mevcut olmadığından image build yerel olarak denenmedi.

systemd örneği `deploy/tradr.service` içindedir. Kod/venv `/opt/tradr`, yapılandırma `/etc/tradr/config.json`, secret environment dosyası `/etc/tradr/secrets.env`, state `/var/lib/tradr` olarak kurulmalıdır. Config'te `state_dir=/var/lib/tradr`, profile/boost yolları `/opt/tradr/profiles.json` ve `/opt/tradr/boosts.json` olmalıdır. `tradr` service kullanıcısının state dizinine yazma, config/secret dosyalarına okuma erişimi olmalıdır; secrets dosyasını `root:tradr` ve `0640` izinleriyle tutun. Örnek servis paper modundadır; canlı için ExecStart'ta `--mode live` açıkça değiştirilir. Otomatik restart kapalıdır.

## Doğrulama ve sınırlar

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m pip check
```

Windows temp dizini erişilemiyorsa pytest'e yeni bir proje içi `--basetemp .reference/pytest-run` verilebilir. CI Windows/Linux testleri, yerel native signer, economic/boost gates, kısmi/geç fill, iptal yarışı, kayıp yanıt, restart ve günlük limit persistence'ını kapsar. Paper modelinde gecikme, sınırlı defter derinliği, maker kısmi katılımı ve saatlik funding vardır; gerçek queue önceliğini/gelecekteki funding'i ispatlamaz. Offline paper funding geçmişi eksikse pozisyonları sürdürmek yerine durur.

Resmi kaynaklar: [Arcus API](https://docs.arcus.xyz/api-reference/introduction.md), [Arcus imzalama](https://docs.arcus.xyz/api-reference/authentication.md), [Arcus RWA](https://docs.arcus.xyz/concepts/perpetuals/real-world-assets.md), [Lighter RH](https://apidocs.lighter.xyz/docs/lighter-rh.md), [Lighter emirleri](https://apidocs.lighter.xyz/docs/trading.md), [Lighter kampanya şartları](https://docs.lighter.xyz/points-program/lighter-on-robinhood-chain-points.md).
