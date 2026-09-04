# VPS Prodüksiyon Dağıtım Runbook'u (M18)

Bu runbook, Phonak Funnel Copilot'u kendi VPS'inize (`ssh vps` ile
eriştiğiniz makine) dağıtmak için baştan sona, kopyala-yapıştır
çalıştırılabilir talimatlar içerir. Hedef: **funnel.vmgorken.com**
(Cloudflare Tunnel üzerinden). Bu ajan VPS'e SSH ile erişemiyor — aşağıdaki
her adımı siz `ssh vps` ile bağlanıp kendiniz çalıştırmalısınız.

Genel M2-döneminden kalma `docs/deploy_guide.md` hâlâ geçerlidir (lokal
çalıştırma, Databricks geçişi vb.) ama Docker/Compose detayları burada
güncellenmiş hâliyle anlatılır — `env_file: .env`, salt-loopback port
bağlama ve `datagen` profili gibi M18 değişiklikleri sadece bu belgede.

---

## 0. Ön koşul kontrolü

```bash
ssh vps
docker --version && docker compose version
cloudflared --version
```

- `docker`/`docker compose` yoksa: [Docker'ın resmi kurulum betiği](https://get.docker.com)
  (`curl -fsSL https://get.docker.com | sh`) veya dağıtımınızın paket
  yöneticisi.
- `cloudflared` yoksa, Bölüm 4'te kurulumu var — şimdilik atlayın.
- Zaten `vmgorken.com` için çalışan bir Cloudflare Tunnel'ınız varsa
  (örn. ana site için), **yeni bir tünel açmanıza gerek yok** — Bölüm 4.3
  o tünele tek bir ingress kuralı eklemeyi anlatıyor.

---

## 1. Repoyu çekme

```bash
git clone https://github.com/gorkenvm/Funnel-text2SQL-copilot.git
cd Funnel-text2SQL-copilot
```

Güncelleme (yeniden dağıtım) sırasında bu adımın yerine Bölüm 5'teki
`git pull` kullanılır — depoyu tekrar klonlamanıza gerek yok.

---

## 2. `.env` dosyasını oluşturma

Sırlar asla koda veya git'e girmez — `.env` `.gitignore`'da zaten hariç
tutulmuş. Dosyayı VPS'te elle oluşturun:

```bash
cat > .env <<'EOF'
# ---- LLM (en az biri; hiçbiri yoksa deterministik KeywordLLM'e düşer) ----
OPENAI_API_KEY=
# ANTHROPIC_API_KEY=

# ---- Demo kapısı (M13) — GERÇEK parolayı asla buraya örnek olarak
# yapıştırmayın; kendi seçtiğiniz, kimseyle paylaşmadığınız bir parola
# yazın. Boş bırakılırsa kilit ekranı hiç görünmez (herkese açık demo). ----
DEMO_PASSPHRASE=<kendi-parolanizi-buraya-yazin>

# ---- Veri sürücüsü ----
AGENT_DB=duckdb

# ---- Databricks (opsiyonel — AGENT_DB=databricks ise gerekli) ----
# DATABRICKS_SERVER_HOSTNAME=
# DATABRICKS_HTTP_PATH=
# DATABRICKS_TOKEN=
# DATABRICKS_CATALOG=workspace
# DATABRICKS_SCHEMA=sonova
EOF

chmod 600 .env
```

`chmod 600` ile dosya sadece bu kullanıcı tarafından okunabilir hâle gelir
— aynı VPS'teki diğer kullanıcılar `.env`'i okuyamaz.

---

## 3. Veri üretimi + uygulamayı ayağa kaldırma

Sentetik veri imaja gömülü değildir — `data/` bir volume'dur, ayrı
üretilir (bir kerelik; veri şemasını değiştirmediğiniz sürece tekrar
çalıştırmanız gerekmez):

```bash
docker compose --profile datagen run --rm datagen
```

Bu, `data/web_events.parquet`, `data/app_events.parquet` ve
`data/id_bridge.parquet` dosyalarını host'taki `./data` klasörüne yazar
(yaklaşık 100.000 kullanıcılık sentetik hunideki veri, birkaç saniye
sürer).

Ardından uygulamayı build edip arka planda başlatın:

```bash
docker compose up -d --build
```

Sağlık kontrolü:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

`"locked": true` görüyorsanız `DEMO_PASSPHRASE` ayarlıdır (beklenen);
`"status": "ok"` her durumda görünmelidir. Port sadece `127.0.0.1`'e
bağlıdır (`docker-compose.yml`'de `127.0.0.1:8000:8000`) — VPS'in genel
arayüzünden bu port erişilemezdir; dışarıya sadece Cloudflare Tunnel
(Bölüm 4) açar.

### Runtime dosya haritası (referans)

`Dockerfile` imaja tam olarak şunları koyar — hiçbiri eksik değil,
hiçbiri fazladan değil (her satır `src/agent` ve `app` içindeki
`open()`/`read_text()`/`Path(__file__)`-göreli okuma noktalarına karşı
tek tek doğrulandı, M18):

| İmaja kopyalanan | Neyi besliyor |
|---|---|
| `src/` | `agent.py`, `agentic.py`, `db.py`, `llm.py`, `dashboard.py`, `medallion.py`, `sentinel_core.py`, `knowledge.py`; `metrics.yaml` `src/agent/` içinde yaşıyor |
| `app/` | `main.py` + servis ettiği `static/` frontend |
| `sql/` | `sql/medallion.sql` (`agent.medallion`) ve `sql/sentinel/*.sql` (`agent.sentinel_core`) |
| `config/` | `model_tiers.json` (`agent.llm`), `dashboard_kpis.json` (`agent.dashboard`), `sentinel_registry.json` (`agent.sentinel_core`) |
| `docs/knowledge/` | RAG bilgi tabanı markdown'ları (`agent.knowledge`) — `docs/`'un geri kalanı **değil** |
| `requirements.txt` | build-time `pip install` |

İmaja **kopyalanmayan**: `tests/`, `reports/`, `notebooks/`, `scripts/`,
`docs/`'un `knowledge/` dışı kalanı, `README.md`, `.env` — çalışan
uygulama bunların hiçbirini diskten okumuyor. `data/` da imaja gömülmez;
her zaman ayrı bir volume'dur (Bölüm 3).

---

## 4. Cloudflare Tunnel: `funnel.vmgorken.com` rotası

### 4.1 `cloudflared` kurulumu (yoksa)

```bash
curl -L --output cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
```

### 4.2 Mevcut tünelinizi kullanma (önerilen) vs. yeni tünel

**ÖNCE tünelinizin nasıl yönetildiğine bakın** — iki mod var ve adımlar
tamamen farklı:

- **Token ile çalışan (dashboard'dan yönetilen) tünel** — cloudflared
  `--token ...` argümanıyla (genelde bir container olarak) çalışıyorsa
  `config.yml` OKUNMAZ; 4.3'ü atlayın. Rota Cloudflare **Zero Trust →
  Networks → Tunnels → tüneliniz → Public Hostname / Published
  application → Add** ekranından eklenir: Subdomain `phonak`, Domain
  `vmgorken.com`, Service Type `HTTP`, URL = **uygulama container'ının
  adı**, ör. `http://phonak_copilot:8000` (cloudflared da bir container
  olduğundan `localhost` ONUN kendi localhost'udur, host'a gitmez —
  uygulama container'ı cloudflared ile aynı docker ağına eklenmiş
  olmalı). DNS kaydı bu ekrandan otomatik oluşur; 4.4 gerekmez.
  ⚠ Tünel token'ı herhangi bir komut çıktısına yazıldıysa işi bitirince
  aynı ekrandan **Rotate token** yapın.

- **`config.yml` ile çalışan (yerel yönetimli) tünel** — 4.3 ve 4.4
  aynen geçerli; mevcut tünele tek ingress kuralı ekleyin.

**Hiç tünel yoksa**, önce oluşturun:

**Hiç tünel yoksa**, önce oluşturun:

```bash
cloudflared tunnel login
cloudflared tunnel create phonak-funnel-copilot
```

Bu komut `~/.cloudflared/<TUNNEL_ID>.json` kimlik dosyasını üretir ve
`<TUNNEL_ID>`'yi ekrana yazar — bir sonraki adımda kullanacaksınız.

### 4.3 `config.yml`'e ingress kuralı ekleme

`~/.cloudflared/config.yml` içinde (mevcut tünelinize ekliyorsanız,
`ingress:` listesinin başına yeni bir madde ekleyin; `service:
http_status:404` her zaman listenin EN SONUNDA kalmalı):

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/<kullanici>/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: funnel.vmgorken.com
    # cloudflared HOST üzerinde çalışıyorsa localhost doğrudur;
    # bir CONTAINER olarak çalışıyorsa uygulama container'ının adını
    # kullanın (http://phonak_copilot:8000) ve iki container'ı aynı
    # docker ağına koyun.
    service: http://localhost:8000
  # ... vmgorken.com için zaten var olan diğer ingress kurallarınız ...
  - service: http_status:404
```

### 4.4 DNS rotası ve servis olarak başlatma

```bash
cloudflared tunnel route dns <TUNNEL_ID> funnel.vmgorken.com

# ön planda test için:
cloudflared tunnel run <TUNNEL_ID>

# kalıcı systemd servisi olarak (zaten kuruluysa bu adımı atlayın —
# config.yml değişikliği restart ile devreye girer):
sudo cloudflared service install
sudo systemctl restart cloudflared   # zaten kuruluysa: restart, ilk kurulumsa: enable --now
```

Doğrulama:

```bash
curl -s https://funnel.vmgorken.com/health | python3 -m json.tool
```

`app/main.py`'deki CORS ayarları zaten `https://funnel.vmgorken.com`,
`https://vmgorken.com` ve `https://www.vmgorken.com` origin'lerine izin
verir — ek bir yapılandırma gerekmez.

---

## 5. Güncelleme (yeni sürüm dağıtma)

```bash
ssh vps
cd Funnel-text2SQL-copilot
git pull
docker compose up -d --build
```

Veri şeması/üretici değişmediyse `datagen` adımını tekrarlamanıza gerek
yok — mevcut `data/*.parquet` kalır.

---

## 6. Geri alma (rollback)

```bash
docker compose down
```

Bir önceki sürüme dönmek için: `git log --oneline` ile önceki commit'i
bulun, `git checkout <önceki-commit>`, sonra `docker compose up -d --build`.
`cloudflared` tüneli konteynerden bağımsız çalıştığı için rollback
sırasında dokunmanıza gerek yok — tekrar `docker compose up -d --build`
yapılana kadar `funnel.vmgorken.com` sadece 502/bağlantı hatası verir.

---

## 7. Dağıtım sonrası sağlık kontrolü (kontrol listesi)

Tarayıcıda `https://funnel.vmgorken.com` açın ve sırayla doğrulayın:

- [ ] `DEMO_PASSPHRASE` ayarlıysa: sayfa açılır açılmaz kilit ekranı
      görünür (kadranlar/grafikler görünmeden önce).
- [ ] Yanlış bir parola girildiğinde durağan, jenerik bir hata mesajı
      görünür (parolanın doğru olup olmadığına dair ipucu vermez).
- [ ] Doğru parolayla kilit ekranı kapanır ve ana arayüz görünür.
- [ ] Sol taraftaki hazır sorulardan birine ("Which channel has the best
      D30 retention?" vb.) tıklayınca cevap + grafik gelir.
- [ ] "Top-12 KPI kokpiti kur" butonu 12 kart üretir.
- [ ] "Son 3 ay için KPI kokpiti" gibi doğal dil filtreli bir soru,
      filtre etiketiyle birlikte kokpiti günceller.
- [ ] `curl -s https://funnel.vmgorken.com/health` → `"status": "ok"`.

Hepsi geçiyorsa dağıtım tamamdır.
