# Öneriler — Nicel Kanıt Tabanı (Backlog #18, taslak)

*Yönetici incelemesi için. Tüm rakamlar `data/*.parquet` üzerinden, `sql/medallion.sql`'in TEK KAYNAK SQL'i `src/agent/medallion.py::apply_medallion()` ile bellek-içi DuckDB'ye birebir uygulanarak (uygulamanın kendisinin açılışta yaptığı yöntem) yeniden hesaplandı — `data/_ground_truth.parquet`'e hiç dokunulmadı. Doğrulama sorgularının tamamı ve ham çıktıları ekte (bkz. "Ek — Doğrulama Sorguları"). Veri penceresi 2026-06-02 → 2026-08-30 (89 gün, ~1 çeyrek) — aşağıdaki tüm "çeyreklik" büyüklükler bu pencerenin ölçeğinde, ekstra bir zaman dilimi varsayımı olmadan.*

*Popülasyon notu: web huni rakamları (`silver.web_user_stages`, `gold.completion_by_channel_device`) TÜM web kullanıcılarını kapsar (rıza/bridge kısıtı yok). Kanal bazlı indirme/eşleşme/D30 ve atıf rakamları (`silver.linked_journeys`, `gold.pairing_by_channel`, `gold.d30_by_channel`, `gold.attribution_first_vs_last`) yalnız rıza vermiş + oturum açmış + köprülenmiş (bridge-linked) 6.863 kullanıcıyı kapsar — bu, 23.405 indirmenin %29,3'ü. Bu ayrım her bulguda açıkça belirtilecek.*

---

## Öneri 1 — Pazarlama: TikTok hacim/kalite açığı ve first-touch/last-touch atıf makası

### Bulgu

TikTok, test başlangıçlarının %16,6'sını (16.613 / 100.000) tek başına oluşturuyor — retargeting_meta + paid_search_brand toplamından (20.951'in altında ama tek kanal olarak organic_direct'ten sonra en büyük hacimli ikinci kanal, paid_social_meta'dan sonra). Buna karşın, aşağıdaki her huni/kalite metriğinde son sırada:

| Kanal | Test başlangıcı | Tamamlama oranı | Bağlanabilir indirme | Eşleştirme oranı | D30 elde tutma |
|---|---:|---:|---:|---:|---:|
| paid_social_tiktok | 16.613 | **%15,2** (en düşük) | 720 | **%41,0** (en düşük) | **%34,3** (en düşük) |
| paid_social_meta | 29.409 | %18,0 | 1.452 | %58,1 | %47,6 |
| organic_direct | 32.927 | %51,2 | 3.152 | %69,4 | %58,4 |
| paid_search_brand | 11.103 | %51,7 | 895 | %66,7 | %54,7 |
| retargeting_meta | 9.948 | %52,8 | 644 | %59,6 | %45,9 |

*SQL tanımı: tamamlama oranı = `gold.completion_by_channel_device` (last-touch kanal etiketi, tüm web nüfusu, kanala göre GROUP BY, `SUM(users_completed)/SUM(users_started)`). Eşleştirme/D30 = `gold.pairing_by_channel` × `gold.d30_by_channel` (first-touch `acquisition_channel`, yalnız bridge-linked 6.863 nüfus, D30 payda = sağdan-sansürlenmemiş 14.533 cihaz).*

Doğrulama: TikTok'un düşük tamamlama oranı tek bir pazarın artefaktı değil — DE %18,2, UK %18,3, US %17,7 (üç pazarda da tutarlı, bkz. Ek). Yani bu, pazar karışımından değil kanalın kendisinden kaynaklanan gerçek bir örüntü.

**İkinci ve bağımsız bulgu — atıf modeli değişince kazanan/kaybeden yer değiştiriyor:**

| Kanal | First-touch | Last-touch | Δ | % değişim |
|---|---:|---:|---:|---:|
| paid_social_meta | 1.452 | 639 | **−813** | **−%56,0** |
| paid_social_tiktok | 720 | 336 | −384 | −%53,3 |
| organic_direct | 3.152 | 3.415 | +263 | +%8,3 |
| paid_search_brand | 895 | 1.229 | +334 | +%37,3 |
| retargeting_meta | 644 | 1.244 | **+600** | **+%93,2** |

*SQL tanımı: `gold.attribution_first_vs_last` — aynı 6.863 kişilik bağlanabilir popülasyon iki kez sayılıyor (first-touch = `id_bridge.acquisition_channel`, last-touch = web oturumunun `utm_campaign`'i, aynı kanal sözlüğüne eşlenmiş). İki modelin toplamı da 6.863 — invaryant doğrulandı (bkz. Ek).*

retargeting_meta first-touch'ta en zayıf görünen kanal (644, en düşük first-touch kredisi) ama last-touch'ta ikinci en güçlü kanala (1.244) sıçrıyor — yani bu kanal talep yaratmıyor, TikTok/Meta'nın yarattığı talebi *kapatıyor*. paid_social_meta ise tam tersi: first-touch'ta en yüksek kredi (1.452) ama last-touch'ta en düşük (639) — %56 kredi kaybı. İki "keşif" kanalı (Meta+TikTok) last-touch'ta tam 1.197 indirme kredisi kaybediyor, üç "kapatıcı" kanal (retargeting, brand-search, organic) tam olarak aynı 1.197'yi kazanıyor — bu, 6.863 kişilik bağlanabilir nüfusun %17,4'ünün kredisinin iki model arasında yer değiştirmesi demek.

**Slayt cümlesi (EN, answer-first):**
> "TikTok is last on completion, pairing, and D30 under every attribution model we tried — while paid_social_meta loses 56% of its download credit switching from first-touch to last-touch, because retargeting_meta closes the demand it creates (+93%)."

### Fırsat büyüklüğü (muhafazakâr → iyimser aralık)

**Varsayımlar (açıkça muhafazakâr):**
- TikTok'un mevcut hacmi (16.613 başlangıç) sabit tutuluyor; bütçenin bir kısmının hacim olarak brand_search/retargeting'e kaydırıldığı, o hacmin brand_search+retargeting'in *harman* verimliliğiyle (tamamlama %52,3, bağlanabilir-indirme-oranı-başlangıç-başına %7,3, eşleştirme %50,9) dönüştüğü varsayılıyor — TikTok-özel maliyet-başına-edinim (CAC) verisi veri setinde YOK, bu yüzden dolar bazlı ROI değil yalnız hacim bazlı bir sayım yapılıyor.
- Yalnız **rıza vermiş + bağlanabilir (bridge-linked)** ölçülebilir kısım sayılıyor — yani bu, "ne kadar daha fazla indirme/eşleştirme *ölçebiliriz*" sorusunun cevabı, toplam nüfusun tahmini değil (bkz. Öneri 3'teki %29,3 kapsam notu).
- Aktarılan hacmin TikTok'ta kalsaydı üreteceği indirme/eşleştirme, gerçek TikTok oranlarıyla hesaplanıp NET kazançtan düşülüyor (çifte sayım yok).

| Senaryo | Aktarılan TikTok başlangıcı | Net bağlanabilir indirme kazancı/çeyrek | Net eşleştirme kazancı/çeyrek |
|---|---:|---:|---:|
| Muhafazakâr (%15 kaydırma) | 2.492 | **+74** | **+48** |
| İyimser (%30 kaydırma) | 4.984 | **+148** | **+97** |

*Not: Bu rakamlar küçük görünüyor çünkü yalnız bağlanabilir (%29,3) alt nüfusu sayıyoruz — dürüstlük gereği böyle raporlanıyor, şişirilmiş bir toplam-nüfus tahmini verilmiyor. Tam nüfuza orantılı ölçeklenirse (yani bağlanabilirlik oranı sabit kalırsa) rakamlar ~3,4 kat büyür, ama bu ekstra bir varsayım olduğu için ana rakam olarak verilmiyor.*

### Bulanıklık/uyarılar (caveats)

- **CAC/harcama verisi yok**: `web_events`, `app_events`, `id_bridge` şemalarında hiçbir maliyet/harcama alanı yok (şema doğrulandı, bkz. Ek). Dolar bazlı ROI hesaplanamaz — bu tamamen hacim bazlı bir sayım.
- **Kanal tanım uyuşmazlığı**: tamamlama oranı tablosu last-touch kanal etiketini kullanıyor (`gold.completion_by_channel_device`), eşleştirme/D30/atıf tabloları first-touch `acquisition_channel` kullanıyor. Aynı "TikTok" adı iki farklı popülasyonu (last-touch oturum vs first-touch edinim) işaret ediyor — tabloya göre birebir aynı kullanıcı kümesi değil, `sql/medallion.sql` içinde açıkça belgelenmiş bir tasarım kararı.
- **Yeniden tahsis verimliliği doğrulanmadı**: aktarılan hacmin gerçekten brand_search/retargeting'in ortalama oranıyla döneceği varsayımı test edilmedi (A/B deneyi önerilir, aşağıda).
- Bağlanabilir popülasyon rastgele değil (bkz. Öneri 3) — bu sayım da o seçilim yanlılığını taşıyor.

### Önerilen aksiyon

1. TikTok harcamasını kısıtla/azalt — tamamlama, eşleştirme VE D30'da, her atıf modelinde son sırada; kazandığı hiçbir mercek yok.
2. paid_social_meta'yı yalnız last-touch ROAS ile değerlendirmeyi bırak; harmanlı (first+last touch ağırlıklı) bir bütçe metriği kur, retargeting_meta'nın bütçesini Meta+TikTok'un ürettiği keşif hacmiyle orantılandır.
3. Bunu tek seferlik kesinti değil, zaman/pazar sınırlı bir A/B bütçe kaydırma deneyi olarak çalıştır — yukarıdaki +74/+148 tahminini gerçek veriyle doğrula.

---

## Öneri 2 — Ürün: Mobil tamamlama açığı gerçek, kanal karışımı yanılsaması değil

### Bulgu

Masaüstü, işitme testini mobilin neredeyse iki katı oranda tamamlıyor; mobil ise TÜM web trafiğinin %68,5'i — yani bu tek açık, huninin çoğunluğunu etkiliyor.

| Cihaz | Test başlangıcı | Trafik payı | Tamamlama oranı |
|---|---:|---:|---:|
| Desktop | 27.755 | %27,8 | %50,6 |
| Tablet | 3.730 | %3,7 | %39,7 |
| Mobile | 68.515 | %68,5 | **%29,4** |

*SQL tanımı: `gold.completion_by_channel_device`, cihaza göre `SUM(users_started)/SUM(users_completed)` — tüm web nüfusu, bridge kısıtı yok.*

**Simpson paradoksu kontrolü** (`docs/knowledge/insights.md`'deki metodoloji birebir uygulandı): paid-social trafiği ağırlıklı mobil (Meta %82,9, TikTok %82,7 mobil payı, diğer kanallarda %55,7–57,7) — bu gerçek bir karışım riski. Kanal × cihaz çapraz tablosuna bakıldığında:

| Kanal | Desktop tamamlama | Mobile tamamlama | Cihaz açığı (pp) |
|---|---:|---:|---:|
| organic_direct | %59,0 | %46,2 | 12,8 |
| paid_search_brand | %58,7 | %47,3 | 11,4 |
| paid_social_tiktok | %24,7 | %13,5 | 11,3 |
| paid_social_meta | %27,3 | %16,3 | 11,1 |
| retargeting_meta | %56,7 | %50,6 | 6,1 |

**Sonuç: Simpson tersinmesi YOK.** Cihaz açığı (6,1–12,8pp) her kanalın İÇİNDE tutarlı şekilde hayatta kalıyor; kanal açığı (~30–34pp, örn. organic desktop %59,0 vs paid_social_tiktok desktop %24,7) her cihazın içinde de hayatta kalıyor. `insights.md`'nin öngördüğü gibi: hem trafik-kalitesi hem mobil-UX hipotezi bağımsız olarak doğru ve büyük ölçüde toplamsal — paid-social×mobil hücreleri (TikTok %13,5, Meta %16,3) "en kötü ikisinin birleşimi", biri diğerini açıklamıyor.

**Slayt cümlesi (EN, answer-first):**
> "Mobile completes hearing tests at half desktop's rate on 68% of our traffic — and this is not a channel-mix illusion: the 6–13pp device gap survives inside every single channel."

### Fırsat büyüklüğü (muhafazakâr → iyimser aralık)

**Varsayımlar:** Cihaz bazında indirme/eşleştirme kırılımı veri setinde yok (app olayları web `device_category`'sini taşımıyor) — bu yüzden ek tamamlamalar, mevcut *harman* huni oranlarıyla (`gold.step_conversion`: tamamlama→indirme %55,6, indirme→eşleştirme %62,4, eşleştirme→D30 %51,1 [sansürlenmemiş]) aşağı taşınıyor; bu açıkça bir yaklaşıklık olarak belirtiliyor.

| Senaryo | Kapatılan açık | Ekstra tamamlama/çeyrek | Ekstra indirme/çeyrek | Ekstra eşleştirme/çeyrek | Ekstra D30/çeyrek |
|---|---:|---:|---:|---:|---:|
| Muhafazakâr (açığın 1/3'ü) | 7,04pp | +4.821 | +2.683 | +1.673 | +855 |
| **Temel senaryo (açığın yarısı)** | 10,56pp | **+7.232** | **+4.024** | **+2.510** | +1.283 |
| İyimser (açığın 2/3'ü) | 14,07pp | +9.642 | +5.365 | +3.347 | +1.710 |

*D30 uyarısı: bu zincirin en spekülatif halkası — huninin D30 oranı yalnız sağdan-sansürlenmemiş (34 günlük pencere kapanmış) 14.533/23.405 (%62,1) cihaz üzerinden hesaplanıyor; 8.872 cihaz (%37,9) henüz gözlemlenemez durumda ve orandan hariç.*

### Kanal karışımıyla etkileşim

Yukarıdaki Simpson kontrolü zaten bunu cevaplıyor: açık kanal-karışımı artefaktı DEĞİL, ama paid-social×mobil kombinasyonu (en düşük iki hücre) UX yatırımı için en yüksek öncelikli segment — hem trafik kalitesi hem cihaz sürtünmesi aynı anda kötü olduğu için oradaki iyileştirme en yüksek marjinal getiriyi verir.

### Bulanıklık/uyarılar (caveats)

- Cihaz bazında indirme/eşleştirme/D30 verisi yok — huni aşağısı tamamen harman oranlarla projekte edildi, kanala/cihaza özgü gerçek dönüşüm farklı olabilir.
- D30 rakamı sansürleme kaynaklı ciddi belirsizlik taşıyor (yukarıda not edildi).
- Bu sentetik veri; gerçek kullanıcı davranışında mobil UX sürtünmesinin (izin ekranları, form uzunluğu, oturum kesintileri) payı burada modellenen toplamsal etkiden farklı olabilir.

### Önerilen aksiyon

Mobil işitme-testi akışında UX denetimi ve yeniden tasarım — paid-social×mobil segmentine (TikTok %13,5, Meta %16,3 — tüm tablodaki en düşük iki hücre) öncelik vererek; yeniden tasarlanan akışı tam yayından önce A/B test et.

---

## Öneri 3 — Ölçüm: Rıza/bağlanabilirlik açığı ve DE'nin görünmezliği

### Bulgu

Kimlik köprüsü (`id_bridge`) — web oturumunu app cihazına bağlayan TEK mekanizma — yalnız 6.863 kullanıcıyı kapsıyor. 23.405 toplam app indirmesinin **%29,3'ü** bağlanabilir; geri kalan **%70,7'si** kanal atıfı, kanala-göre-eşleştirme ve kanala-göre-D30 ölçümüne tamamen görünmez.

Bağlanabilirlik payı pazara göre keskin ve monoton biçimde değişiyor:

| Pazar | Redirect kullanıcısı | Bridge kullanıcısı | Bağlanabilirlik payı |
|---|---:|---:|---:|
| DE | 9.210 | 1.963 | **%21,3** |
| UK | 5.947 | 1.726 | %29,0 |
| US | 8.248 | 3.174 | **%38,5** |

*Doğrulandı: DE %21,3 / UK %29,0 / US %38,5 — görevde verilen rakamlarla birebir eşleşiyor. SQL tanımı: `gold.linkable_share_by_market` = bridge kullanıcısı / app-store-redirect'e ulaşan web kullanıcısı (pazar bazında; app olayları pazar taşımadığı için redirect vekil payda olarak kullanılıyor — belgelenmiş bir yaklaşıklık).*

**DE özelinde ne anlama geliyor**: DE'de app indiren ~7.247 kullanıcının (9.210 redirect − 1.963 bridge ≈ tahmini) atıf, eşleştirme-kanalı ve D30 performansı ÖLÇÜLEMİYOR — DE için raporlanan her "hangi kanal en iyi eşleştiriyor" veya "DE'de D30 nedir" cevabı, DE'nin sadece görünür beşte-birlik diliminin cevabı, DE'nin gerçeğinin değil.

**Slayt cümlesi (EN, answer-first):**
> "We can only measure post-download performance for 29% of users overall — and in Germany that drops to 21%, so today's DE channel and retention numbers describe a fifth of the market, not the market."

### Fırsat büyüklüğü (muhafazakâr → iyimser aralık)

**Varsayımlar:** Bu bir ölçüm-kapsamı/görünürlük fırsatı, dönüşüm fırsatı değil — gelir değil, "ölçülebilir hale gelen ek yolculuk sayısı" olarak boyutlandırılıyor. Hedef, UK'nin payı (%29,0) — US değil, çünkü UK, DE gibi GDPR/opt-in rejimi altında (bkz. `docs/knowledge/privacy.md`); US'in %38,5'i kısmen CCPA'nın opt-out varsayılan rejiminden kaynaklanıyor, yani DE'yi doğrudan US'e hedeflemek adil bir kıyas değil.

| Senaryo | Hedef bağlanabilirlik payı | Ekstra ölçülebilir DE yolculuğu/çeyrek |
|---|---:|---:|
| Muhafazakâr (UK'ye olan açığın yarısı) | %25,2 | **+355** |
| İyimser (UK ile tam parite) | %29,0 | **+710** |
| *(Referans, önerilmiyor — US paritesi)* | %38,5 | *(+1.581, rejim farkı nedeniyle hedef olarak önerilmiyor)* |

### Önerilen çerçeve — GDPR-uyumlu, ASLA "rızasız daha fazla izleme" değil

Bu bir izleme-genişletme önerisi DEĞİL. Önerilen: **rıza-UX iyileştirmesi** (DE'ye özel, ilk app açılışındaki oturum-açma/kimlik-bağlama isteminin A/B testi — daha net dil, daha az sürtünme, doğru zamanlama) + **sunucu-taraflı/consent-mode tarzı ölçüm** (kullanıcı rıza verdiğinde ölçümün güvenilir şekilde toplanması, üçüncü-taraf çerez kaybına dayanıklı) — rıza vermeyen kullanıcıyı asla izlemeden. Amaç, rıza verme ORANINI artırmak, rızasız veri toplamayı değil.

### Bulanıklık/uyarılar (caveats) — dürüstlük notu

- **Rejim etkisi ayrıştırılamıyor**: `docs/knowledge/privacy.md`'nin öngördüğü gibi, DE'nin en düşük payı kısmen düzenleyici varsayılan farkından (opt-in vs opt-out) kaynaklanıyor olabilir, tamamen ürün-UX sürtünmesinden değil. Eldeki veriden bu ikisi ayrıştırılamıyor — "%21,3'ün ne kadarı UX ile düzeltilebilir, ne kadarı yasal zeminin kendisi" sorusunu **doğrulayamadım**; öneri, en azından bir kısmının UX-adreslenebilir olduğunu varsayıyor ama bunu veri kanıtlamıyor.
- Bağlanabilir popülasyon rastgele değil — daha meşgul, markaya daha güvenen kullanıcıları temsil ediyor (`privacy.md`); bu nedenle bugün raporlanan her kanal-kalitesi/elde-tutma rakamı görünür alt-nüfusun rakamı, tüm nüfusun değil.
- `opt_in_flag`, `id_bridge` içinde %100 `true` (veri setinin kurgusu gereği köprü zaten yalnız rıza verenleri içeriyor) — yani "rıza oranı" bu tablodan doğrudan okunamıyor, ancak redirect/bridge oranı üzerinden vekil olarak ölçülüyor.

---

## Doğrulanamayan/kısmen doğrulanan planted-pattern kontrolleri (dürüstlük notu)

Görevde işaret edilen ve `docs/knowledge/insights.md`'de tarif edilen örüntülerin tümü şu şekilde ele alındı:

- ✅ **Mobil UX vs trafik kalitesi** — her ikisi de bağımsız olarak doğrulandı (Öneri 2), Simpson tersinmesi yok.
- ✅ **TikTok hacim/kalite açığı** — doğrulandı, üç pazarda da tutarlı (Öneri 1).
- ✅ **First-touch/last-touch atıf makası** (retargeting zayıf-first/güçlü-last, Meta tersi) — doğrulandı (Öneri 1).
- ✅ **DE < UK < US bağlanabilirlik sıralaması** — birebir doğrulandı (Öneri 3).
- ⚠️ **DOĞRULANAMADI**: DE'nin düşük bağlanabilirlik payının ne kadarının düzenleyici rejim farkından (opt-in varsayılan), ne kadarının gerçek ürün/UX sürtünmesinden kaynaklandığı. Veri setinde bunu ayrıştıracak bir alan (örn. rıza ekranı gösterim/terk oranı, A/B varyant bilgisi) yok — bu, Öneri 3'ün en kırılgan varsayımı olarak açıkça işaretlendi.
- ⚠️ **HESAPLANAMADI**: Pazarlama önerisinin dolar bazlı ROI/CAC boyutu — şemada hiçbir maliyet/harcama alanı yok (`web_events`, `app_events`, `id_bridge` — üçü de doğrulandı, bkz. Ek). Yalnız hacim bazlı bir sayım sunulabildi.
- ℹ️ `insights.md`'deki destek-oturumu/elde-tutma korelasyonu ve iOS/Android pazar-karışımı notları bu üç öneriyle doğrudan ilgili olmadığı için bu turda yeniden test edilmedi (kapsam dışı bırakıldı, unutulmadı).

---

## Ek — Doğrulama Sorguları

*Aşağıdaki sorguların tümü, `sql/medallion.sql`'in `src/agent/medallion.py::apply_medallion()` ile bellek-içi DuckDB'ye uygulanmasıyla (uygulamanın kendi açılış yöntemi, `raw_schema="main"`, üç parquet dosyası view olarak kayıtlı) elde edilen bronze/silver/gold katmanları üzerinde, bu turda yeniden çalıştırıldı. Ham çıktılar aşağıda birebir.*

### Sağlık kontrolü — huni genel görünümü

```sql
SELECT * FROM gold.funnel_overview ORDER BY stage_order
```
```
 stage_order                 stage  users
           1    hearing_test_start 100000
           2 hearing_test_complete  42062
           3          app_download  23405
           4    hearing_aid_paired  14599
           5            active_d30   7461
```

```sql
SELECT * FROM gold.step_conversion
```
```
 stage_order                                        step  from_users  to_users  conversion_rate
           1                                         NaN        <NA>    100000              NaN
           2 hearing_test_start -> hearing_test_complete      100000     42062         0.420620
           3       hearing_test_complete -> app_download       42062     23405         0.556440
           4          app_download -> hearing_aid_paired       23405     14599         0.623756
           5            hearing_aid_paired -> active_d30       14599      7461         0.511062
```

### Öneri 1 — Pazarlama

```sql
SELECT channel,
       SUM(users_started) AS test_starts,
       SUM(users_completed) AS test_completes,
       ROUND(1.0*SUM(users_completed)/NULLIF(SUM(users_started),0), 4) AS completion_rate
FROM gold.completion_by_channel_device
GROUP BY channel
ORDER BY completion_rate ASC
```
```
           channel  test_starts  test_completes  completion_rate
paid_social_tiktok      16613.0          2531.0           0.1524
  paid_social_meta      29409.0          5284.0           0.1797
    organic_direct      32927.0         16875.0           0.5125
 paid_search_brand      11103.0          5744.0           0.5173
  retargeting_meta       9948.0          5257.0           0.5284
```

```sql
SELECT p.acquisition_channel AS channel,
       p.linked_downloads,
       ROUND(p.pairing_rate,4) AS pairing_rate,
       ROUND(d.d30_retention_rate,4) AS d30_retention_rate,
       d.eligible_users AS d30_eligible_users,
       d.retained_users AS d30_retained_users
FROM gold.pairing_by_channel p
JOIN gold.d30_by_channel d ON d.acquisition_channel = p.acquisition_channel
ORDER BY p.pairing_rate ASC
```
```
           channel  linked_downloads  pairing_rate  d30_retention_rate  d30_eligible_users  d30_retained_users
paid_social_tiktok               720        0.4097              0.3431                 443               152.0
  paid_social_meta              1452        0.5806              0.4763                 886               422.0
  retargeting_meta               644        0.5963              0.4593                 418               192.0
 paid_search_brand               895        0.6670              0.5468                 545               298.0
    organic_direct              3152        0.6935              0.5843                1951              1140.0
```

```sql
SELECT * FROM gold.attribution_first_vs_last ORDER BY channel, attribution_model
```
```
           channel attribution_model  attributed_downloads
    organic_direct       first_touch                  3152
    organic_direct        last_touch                  3415
 paid_search_brand       first_touch                   895
 paid_search_brand        last_touch                  1229
  paid_social_meta       first_touch                  1452
  paid_social_meta        last_touch                   639
paid_social_tiktok       first_touch                   720
paid_social_tiktok        last_touch                   336
  retargeting_meta       first_touch                   644
  retargeting_meta        last_touch                  1244
```

```sql
SELECT attribution_model, SUM(attributed_downloads) AS total
FROM gold.attribution_first_vs_last GROUP BY attribution_model
```
```
attribution_model  total
      first_touch 6863.0
       last_touch 6863.0
```

```sql
WITH piv AS (
  SELECT channel,
         MAX(CASE WHEN attribution_model='first_touch' THEN attributed_downloads END) AS first_touch,
         MAX(CASE WHEN attribution_model='last_touch' THEN attributed_downloads END) AS last_touch
  FROM gold.attribution_first_vs_last GROUP BY channel
)
SELECT channel, first_touch, last_touch, (last_touch-first_touch) AS delta,
       ROUND(100.0*(last_touch-first_touch)/NULLIF(first_touch,0),1) AS pct_change
FROM piv ORDER BY delta
```
```
           channel  first_touch  last_touch  delta  pct_change
  paid_social_meta         1452         639   -813       -56.0
paid_social_tiktok          720         336   -384       -53.3
    organic_direct         3152        3415    263         8.3
 paid_search_brand          895        1229    334        37.3
  retargeting_meta          644        1244    600        93.2
```

```sql
SELECT market,
       SUM(test_starts) starts, SUM(test_completes) completes,
       ROUND(1.0*SUM(test_completes)/NULLIF(SUM(test_starts),0),4) rate
FROM gold.web_funnel_daily_cube
WHERE channel = 'paid_social_tiktok'
GROUP BY 1 ORDER BY 1
-- Not: web_funnel_daily_cube'un kanal başına 'completes' payı, ~%6'lık
-- "başlangıcı olmayan tamamlama" gürültüsünü completion_by_channel_device'in
-- aksine dışlamıyor (medallion.sql'de belgelenmiş, kasıtlı bir tasarım
-- farkı) -- bu yüzden mutlak oranlar completion_by_channel_device'ten
-- hafif farklı (~%18 vs %15,2), ama YÖN ve pazarlar-arası TUTARLILIK
-- (üç pazarda da ~%18) sağlam: TikTok'un zayıflığı tek pazar artefaktı değil.
```
```
market             channel  starts  completes    rate
    DE paid_social_tiktok  6539.0     1190.0  0.1820
    UK paid_social_tiktok  4205.0      771.0  0.1834
    US paid_social_tiktok  5869.0     1041.0  0.1774
```

**Fırsat büyüklüğü hesap detayı (harmanlı oranlar):**
```
Harmanlı brand_search+retargeting tamamlama oranı: (5744+5257)/(11103+9948) = 0.5226
brand_search+retargeting bağlanabilir-indirme / başlangıç: (895+644)/(11103+9948) = 0.0731
TikTok bağlanabilir-indirme / başlangıç: 720/16613 = 0.0433
Harmanlı brand_search+retargeting eşleştirme oranı: (298+192)/(545+418) = 0.5088
TikTok eşleştirme oranı: 0.4097

%15 kaydırma (2.492 başlangıç):
  yeni bağlanabilir indirme: 182  |  TikTok'ta kalsaydı: 108  |  net: +74
  yeni eşleştirme: 93  |  TikTok'ta kalsaydı: 44  |  net: +48

%30 kaydırma (4.984 başlangıç):
  yeni bağlanabilir indirme: 364  |  TikTok'ta kalsaydı: 216  |  net: +148
  yeni eşleştirme: 185  |  TikTok'ta kalsaydı: 89  |  net: +97
```

**Şema doğrulaması — maliyet/harcama alanı yok:**
```sql
DESCRIBE bronze.web_events;  DESCRIBE bronze.app_events;  DESCRIBE bronze.id_bridge;
```
```
web_events:  user_pseudo_id, session_id, event_name, event_timestamp, page_location,
             utm_campaign, device_category, country, consent_state
app_events:  hashed_device_id, platform, event_name, event_timestamp, app_version
id_bridge:   hashed_id, market, opt_in_flag, acquisition_channel, web_pseudo_id,
             app_device_id, linked_at
-- Hiçbir tabloda maliyet/harcama/CPI/CAC alanı yok.
```

### Öneri 2 — Ürün

```sql
SELECT device_category, SUM(users_started) AS starts, SUM(users_completed) AS completes,
       ROUND(1.0*SUM(users_completed)/NULLIF(SUM(users_started),0),4) AS completion_rate
FROM gold.completion_by_channel_device GROUP BY 1 ORDER BY completion_rate DESC
```
```
device_category  starts  completes  completion_rate
        desktop 27755.0    14032.0           0.5056
         tablet  3730.0     1480.0           0.3968
         mobile 68515.0    20179.0           0.2945
```

```sql
SELECT device_category, SUM(users_started) AS starts,
       ROUND(100.0*SUM(users_started)/SUM(SUM(users_started)) OVER (),1) AS pct_share
FROM gold.completion_by_channel_device GROUP BY 1 ORDER BY pct_share DESC
```
```
device_category  starts  pct_share
         mobile 68515.0       68.5
        desktop 27755.0       27.8
         tablet  3730.0        3.7
```

```sql
SELECT channel, device_category, users_started, users_completed, ROUND(completion_rate,4) AS completion_rate
FROM gold.completion_by_channel_device ORDER BY channel, device_category
```
```
           channel device_category  users_started  users_completed  completion_rate
    organic_direct         desktop          12912           7619.0           0.5901
    organic_direct          mobile          18327           8466.0           0.4619
    organic_direct          tablet           1688            790.0           0.4680
 paid_search_brand         desktop           4235           2488.0           0.5875
 paid_search_brand          mobile           6337           2998.0           0.4731
 paid_search_brand          tablet            531            258.0           0.4859
  paid_social_meta         desktop           4384           1198.0           0.2733
  paid_social_meta          mobile          24369           3960.0           0.1625
  paid_social_meta          tablet            656            126.0           0.1921
paid_social_tiktok         desktop           2510            621.0           0.2474
paid_social_tiktok          mobile          13744           1851.0           0.1347
paid_social_tiktok          tablet            359             59.0           0.1643
  retargeting_meta         desktop           3714           2106.0           0.5670
  retargeting_meta          mobile           5738           2904.0           0.5061
  retargeting_meta          tablet            496            247.0           0.4980
```

```sql
SELECT channel,
       SUM(CASE WHEN device_category='mobile' THEN users_started ELSE 0 END) AS mobile_starts,
       SUM(users_started) AS total_starts,
       ROUND(100.0*SUM(CASE WHEN device_category='mobile' THEN users_started ELSE 0 END)/SUM(users_started),1) AS mobile_share_pct
FROM gold.completion_by_channel_device GROUP BY 1 ORDER BY mobile_share_pct DESC
```
```
           channel  mobile_starts  total_starts  mobile_share_pct
  paid_social_meta        24369.0       29409.0              82.9
paid_social_tiktok        13744.0       16613.0              82.7
  retargeting_meta         5738.0        9948.0              57.7
 paid_search_brand         6337.0       11103.0              57.1
    organic_direct        18327.0       32927.0              55.7
```

```sql
WITH piv AS (
  SELECT channel,
    MAX(CASE WHEN device_category='desktop' THEN completion_rate END) AS desktop_rate,
    MAX(CASE WHEN device_category='mobile' THEN completion_rate END) AS mobile_rate
  FROM gold.completion_by_channel_device GROUP BY channel
)
SELECT channel, ROUND(desktop_rate,4) desktop_rate, ROUND(mobile_rate,4) mobile_rate,
       ROUND((desktop_rate-mobile_rate)*100,1) AS gap_pp
FROM piv ORDER BY gap_pp DESC
```
```
           channel  desktop_rate  mobile_rate  gap_pp
    organic_direct        0.5901       0.4619    12.8
 paid_search_brand        0.5875       0.4731    11.4
paid_social_tiktok        0.2474       0.1347    11.3
  paid_social_meta        0.2733       0.1625    11.1
  retargeting_meta        0.5670       0.5061     6.1
```

**Fırsat büyüklüğü hesap detayı:**
```
Desktop 0.5056, Mobile 0.2945, açık 21.11pp, yarısı 10.56pp

1/3 açık (7.04pp):  +4.821 tamamlama  ->  +2.683 indirme  ->  +1.673 eşleştirme  ->  +855 D30
1/2 açık (10.56pp): +7.232 tamamlama  ->  +4.024 indirme  ->  +2.510 eşleştirme  ->  +1.283 D30
2/3 açık (14.07pp): +9.642 tamamlama  ->  +5.365 indirme  ->  +3.347 eşleştirme  ->  +1.710 D30
```

### Öneri 3 — Ölçüm

```sql
SELECT
  (SELECT COUNT(DISTINCT hashed_device_id) FROM bronze.app_events) AS total_app_devices,
  (SELECT COUNT(*) FROM bronze.id_bridge) AS bridge_rows,
  (SELECT COUNT(*) FROM silver.v_attribution_eligible) AS eligible_rows
```
```
 total_app_devices  bridge_rows  eligible_rows
             23405         6863           6863
```

```sql
SELECT * FROM gold.linkable_share_by_market ORDER BY linkable_share
```
```
market  redirect_users  bridge_users  linkable_share
    DE            9210          1963        0.213138
    UK            5947          1726        0.290230
    US            8248          3174        0.384821
```

```sql
SELECT opt_in_flag, COUNT(*) FROM bronze.id_bridge GROUP BY 1
```
```
 opt_in_flag  count_star()
        True          6863
```

```sql
SELECT censored, COUNT(*) AS devices, ROUND(100.0*COUNT(*)/SUM(COUNT(*)) OVER (),1) AS pct
FROM silver.app_user_stages GROUP BY 1
```
```
 censored  devices  pct
    False    14533 62.1
     True     8872 37.9
```

**Fırsat büyüklüğü hesap detayı:**
```
DE payı 0.2131, UK payı 0.2902, US payı 0.3848

UK'ye açığın yarısı (hedef pay 0.2517): hedef DE bridge = 2.318  -> ekstra = +355
UK ile tam parite (hedef pay 0.2902):   hedef DE bridge = 2.673  -> ekstra = +710
(Referans, önerilmiyor) US paritesi (0.3848): hedef DE bridge = 3.544 -> ekstra = +1.581
```

### Destekleyici / bağlam sorguları

```sql
SELECT * FROM gold.completion_by_channel ORDER BY completion_rate DESC
```
```
            channel  test_starts  test_completes  completion_rate
   retargeting_meta        9948          5257.0         0.528448
       brand_search       11103          5744.0         0.517338
       organic/none       32927         16875.0         0.512497
summer_hearing_meta       29409          5284.0         0.179673
   tiktok_awareness       16613          2531.0         0.152351
```

```sql
SELECT * FROM gold.pairing_by_platform_market
```
```
market platform  app_devices  paired_devices  pairing_rate
    DE  Android         1190           689.0      0.578992
    DE      iOS          773           502.0      0.649418
    UK  Android          841           506.0      0.601665
    UK      iOS          885           587.0      0.663277
    US  Android         1287           753.0      0.585082
    US      iOS         1887          1268.0      0.671966
```

```sql
SELECT COUNT(*) AS users_with_repeat_start
FROM (
  SELECT user_pseudo_id, COUNT(*) AS n FROM bronze.web_events
  WHERE event_name='hearing_test_start' GROUP BY 1 HAVING COUNT(*) > 1
)
```
```
 users_with_repeat_start
                    7371
```

---

*Yöntem notu: Medallion, `sql/medallion.sql` (tek kaynak SQL dosyası) `src/agent/medallion.py::apply_medallion()` fonksiyonu ile bellek-içi bir DuckDB bağlantısına, uygulamanın `DuckDBDriver` açılışında yaptığı BİREBİR aynı yöntemle (45 ifade, `{{raw}}` → `"main"`) uygulanarak inşa edildi — ayrı/elle yazılmış bir SQL yolu kullanılmadı. Girdi yalnız `data/web_events.parquet`, `data/app_events.parquet`, `data/id_bridge.parquet`; `data/_ground_truth.parquet` hiç okunmadı, hiç referans verilmedi.*
