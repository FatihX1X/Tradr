# Gerçek veriyle paper doğrulaması — 8 Ekim 2026

Arcus `https://api.arcus.xyz` ve Lighter RH `https://api.rh.lighter.xyz` production REST/WebSocket verileri kullanıldı. Fiyat, bid/ask derinliği, mark, oracle ve signed fonlama tahmini canlıydı; hesap bakiyeleri, emir gerçekleşmeleri ve PnL simülasyondu. Gerçek venue emri gönderilmedi.

## Strateji gözlemi

- Platform başına 100 USD sanal teminat, 50 USD bacak limiti, 1 USD günlük paper durdurma eşiği, Standard RH fee modeli.
- Beş dakikalık strateji çalıştırması tamamlandı; sonrasında sürekli paper süreç başlatıldı.
- Gözlem kaydı: **16:44:20–16:48:31 Europe/Istanbul**, 43 erişilebilir snapshot; 42 HTTP 200, bir geçici HTTP 503. Veri/bağlantı kontrolü sonrasında tarama devam etti. Beş bağlantı reddi, süreli süreç sona erdikten sonraki gözlem aralığına aittir.
- BTC için Arcus fiyat/defter 43/43, RH 42/43 örnekte iki saniyelik freshness kontrolünü geçti. Bir RH stale-book örneği işlem dışı kaldı.
- Ana strateji **sıfır emir** açtı: fiyat farkı dört emir maliyetini, 80 baz puan toplam kayma rezervini ve fonlama rezervini karşılamadı. Maliyet rezervi gerçekleşmiş zarar değildir.

Gözlem aralığındaki gerçek bid fiyatları:

| Piyasa | Arcus min–max | Lighter RH min–max |
|---|---:|---:|
| BTC | 81.761,2–82.029,1 USD | 81.750,9–82.013,1 USD |
| ETH | 2.504,12–2.515,36 USD | 2.503,81–2.514,78 USD |

Örnek kaynak zamanında 16:44:31–16:44:32'de BTC Arcus bid/ask 82.011,2 / 82.011,3; RH 82.001,7 / 82.015,1 idi. Defter kaynak yaşları yaklaşık 0,45 ve 0,30 saniyeydi. Hesaplanan net giriş avantajı yaklaşık **−88 / −87 baz puan** olduğu için giriş engellendi.

## Ayrı sanal yürütme tanılaması

Stratejinin ekonomik eşiği değiştirilmedi. İzole paper state üzerinde tek manuel sanal hedge açıp kapatma tanılaması yapıldı; kaynak transport'una GET-only REST koruması eklendi. Bu tanılama, stratejinin ekonomik olarak onayladığı bir işlem değildir.

| Sanal emir | Miktar | Gerçek defterden simüle edilen ortalama fiyat |
|---|---:|---:|
| Arcus al | 0,00030 BTC | 81.833,8 USD |
| RH sat | 0,00030 BTC | 81.801,7 USD |
| Arcus reduce-only sat | 0,00030 BTC | 81.844,8 USD |
| RH reduce-only al | 0,00030 BTC | 81.848,6 USD |

Dört emir filled oldu; iki tarafta da pozisyon sıfırlandı. Sanal toplam sonuç **−0,0218183055 USD**: Arcus komisyonu **0,0110483055 USD**, fiyat farkı/spread etkisi **0,01077 USD**. RH Standard public fee verisi sıfırdı. Bu, gerçek venue'da aynı gerçekleşmenin veya yalnızca fee kaybının garantisi değildir.

## Bulunan ve düzeltilen eksikler

1. Terminalde canlı fiyat yoktu: `run` periyodik fiyat çıktısı, `markets` komutu ve health içinde fiyat/fonlama/veri yaşı eklendi.
2. Paper RH hesabı Premium ücretini varsayıyordu: Standard modda gerçek public `maker_fee`/`taker_fee` verisi kullanılıyor; eksik fee verisinde işlem engelleniyor. Premium modeli açık yapılandırma ile seçiliyor.
3. Flat hesap geçici metadata hatasında kapanıyordu: yeni giriş durdurularak `paused` kalıyor, veri iyileşince tarama devam ediyor. Açık pozisyonda kurtarma davranışı korunuyor.
4. Health, metadata eksikken bağlantı açık diye 200 dönebiliyordu: metadata hazırlığı da readiness kontrolüne eklendi.
5. Gerçek fiyat ile sanal bakiye ayrımı görünmüyordu: execution/data modu ve `balance_source=simulated` etiketleri eklendi. Health journal yazımı saniyede birle sınırlandı.

43 test; bunlar eski veri, signed fonlama, Standard public fee, metadata kesintisi/iyileşmesi ve gözlemlenebilirlik regresyonlarını içerir. Windows/Linux CI kontrolü ilgili commit üzerinde ayrıca çalıştırılır.

## Kalan sınırlar

- Hisse/ETF/emtia ekonomik ve kurumsal olay profilleri doğrulanmadan etkinleştirilmez; gerçek fiyatları yine gözlemlenebilir.
- Robinhood Wallet 2x boost'un bağımsız API botuna uygunluğu doğrulanmadı.
- Gerçek hesap anahtarlarıyla canlı emir denemesi yapılmadı. Funding settlement'ın saat sınırında gerçek platform tutarıyla eşleştirilmesi bu kısa koşuda doğrulanmadı.
- Paper model gerçek queue önceliğini, ağ kopmasındaki fill yarışlarını veya gelecekteki fonlama oranını ispatlamaz.
