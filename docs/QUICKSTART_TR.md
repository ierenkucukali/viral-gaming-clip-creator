# Hızlı başlangıç (Türkçe) — Yerel GTA oyun klip makinesi

Bu klasör, **SharkyClips**'in (MIT) bu bilgisayara özel, yerel bir uyarlamasıdır. Her şey bu PC'de
çalışır: Whisper (GPU), yerel LLM (Ollama, qwen3.5:4b), ffmpeg. **Ücretli API, kredi, filigran,
otomatik paylaşım yok.** Paylaşmadan önce her klibi sen seçersin.

Algılanan sistem: Ubuntu 22.04.5 · i7-11800H · 31 GB RAM · NVIDIA RTX A3000 Laptop (6 GB VRAM,
sürücü 580, CUDA 13 sürücüsü / cu12 kütüphaneleri pip ile) · NVENC H.264 kodlayıcı.

---

## 1) Kurulum ve başlatma (bu bilgisayar, Linux)

Kurulum zaten yapıldı. Sıfırdan gerekirse (sudo gerekmez):

```bash
cd ~/Desktop/gaming-clip
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
.venv/bin/python -m pip install -r backend/requirements-nvidia.txt streamlink
# ffmpeg: proje içine statik build (tools/ffmpeg/bin) — docs/LOCAL_SETUP_REPORT.md'ye bak
(cd frontend && npm ci && npm run build)
```

**Her seferinde başlatmak için:**

```bash
cd ~/Desktop/gaming-clip
./scripts/start.sh            # http://127.0.0.1:8000  (yalnızca bu bilgisayar)
```

Tarayıcıda **http://127.0.0.1:8000** aç → üst menüde **Oyun** sekmesi.
Durdurmak: terminalde `Ctrl+C`.

Kontrol (doctor): `source scripts/env.sh && cd backend && python cli.py doctor --no-fix`

> `scripts/env.sh` şunları ayarlar: proje içindeki ffmpeg'i ve `.venv`'i PATH'e ekler, ayarları
> `~/.sharkyclips` yerine proje içindeki `.sharkyclips/` klasöründe tutar.

## 2) YouTube video (VOD) bağlantısı yapıştırmak

1. **Oyun** sayfası → **YouTube VOD** kartı → bağlantıyı yapıştır → **İçe aktar ve analiz et**.
2. İndirme → ses çıkarma → Whisper (GPU) → highlight analizi → facecam tespiti kendiliğinden sırayla çalışır
   (**Sıra** sekmesinde ilerleme).
3. Sadece indirme iznin olan, herkese açık videolar. Oturum açma, çerez, üyelik/yaş sınırı atlatma **yok**;
   böyle bir videoda net bir hata alırsın — dosyayı kendin indirip **Yerel dosya** kartını kullan.
4. 6–7 saatlik VOD: video bir kez indirilir (yarım kalan indirme kaldığı yerden devam eder, ağ hatasında
   4 deneme / artan bekleme), Whisper 10 dakikalık parçalar halinde önbelleğe yazar (kesilirse kaldığı
   yerden sürer). 7 saat yeniden kodlanmaz; yalnızca seçtiğin klipler render edilir.

Komut satırından da olur:

```bash
source scripts/env.sh && cd backend
python cli.py ingest "https://www.youtube.com/watch?v=VIDEO_ID"
python cli.py transcribe <id-öneki>      # GPU
python cli.py highlights <id-öneki>
```

**Yerel dosya:** Oyun → Yerel dosya → tam yol (ör. `/home/ben/Videolar/yayin.mkv`) → İçe aktar.
MP4/MKV/TS desteklenir; dosya kopyalanmaz, hard-link yapılır.

## 3) Süren (CANLI) bir yayını kaydetmek

1. **Oyun** → **YouTube CANLI** kartı → canlı yayın bağlantısını yapıştır (watch?v=..., /live/..., youtu.be/...).
2. Kalite: **720p** (varsayılan, az disk) veya 1080p. Saklama: **Kayan arabellek** (varsayılan son 90 dk,
   30–120 dk ayarlanır) ya da **Tam arşivi sakla**.
3. "Bu yayını kaydetme iznim var" kutusunu işaretle → **Kaydı başlat**.
4. Kayıt **şu andan itibaren** 6 saniyelik parçalarla, yeniden kodlamadan (`ffmpeg -c copy`) yapılır.
   Kayıt hiçbir zaman analiz/GPU beklemez. Bağlantı koparsa otomatik, sınırlı sayıda yeniden bağlanır.
5. Kayıt sürerken:
   - Bulunan anlar (yüksek ses / bağırma ani yükselmeleri) birkaç saniye içinde **Bulunan anlar** listesinde
     görünür; "Neden seçildi" sebepleri (ani ses +X dB, uzun süre yüksek, konuşma) yanında yazar.
   - **Önizle** → parçayı hemen izle. **Klip yap** → normal bir SharkyClips projesi oluşur (Whisper +
     altyazı + facecam), "9:16'yı hemen render et" işaretliyse MP4 de üretilir.
   - **Anı işaretle** → şu anki anı işaretler (25 sn öncesi + 8 sn sonrası).
   - **Son 30 / 60 / 90 / 120 sn'yi kes** → AI kapalı olsa bile çalışır.
6. Yayın bitince durum "Yayın bitti" olur. **Kayıttan tek proje oluştur** ile diskteki kayıt (yeniden
   kodlamadan) normal bir projeye dönüşür ve tam analiz yapılır.
7. Sunucu kapanırsa tamamlanmış parçalar korunur; oturum "Kesildi" görünür → **Şimdiden devam et**.

## 4) DVR / geri doldurma (backfill) — ne yapabilir, ne yapamaz

- Şimdi başlatılan kayıt, yayının **önceki 6 saatini sihirle yakalayamaz.**
- Oturum sayfasında **Önceki saatleri geri doldur (deneysel)** → `yt-dlp --live-from-start`'ı ayrı bir
  görev olarak dener. Yalnızca yayında DVR açıksa ve YouTube başlangıcı hâlâ sunuyorsa çalışır;
  başarısız olabilir veya şimdiki kayıtla çakışabilir. Başarısızlığı canlı kaydı **asla** durdurmaz,
  durum dürüstçe "failed" ve sebebiyle gösterilir. Uydurma görüntü üretilmez.
- DVR kapalı / kısıtlıysa atlatma yapılmaz: yayıncının VOD'unu bekleyip **YouTube VOD** kartıyla içe aktar.

## 5) 15 klibi bulmak ve incelemek

- Her projede (Kütüphane → proje) aday listesi: skor, sebepler, **Kabul** düğmesi, 9:16 canlı önizleme.
- Render paneli → **Düzen: Kamera + oyun** seçiliyken yeni kontroller:
  **Facecam: Oyunun üstünde / altında**, **Kamera yüksekliği %25–40** (varsayılan %30),
  **Oyun görüntüsü: Kırp / Tam kare + bulanık dolgu (HUD görünür) / Tam kare + siyah şerit**,
  **Kırpma konumu** ve **Oyun içindeki küçük kamerayı gizle**.
- Facecam bulunamazsa uydurulmaz: düzen otomatik olarak sadece oyun (tam kare + bulanık dolgu) olur.
  Facecam editörüyle kutuyu taşı/boyutlandır; kutu yayıncı/kanal + çözünürlük için (normalize koordinatla)
  hatırlanır.
- **Oyun → Klip yapısı ve günlük hedef**: en kısa 15 sn, hedef 30 sn, en uzun 60 sn, öncesi 6–12 sn,
  video başına aday sayısı, günlük hedef (varsayılan 12) ve "zayıf" eşiği. Yeterli iyi aday yoksa
  "Only N good candidates…" uyarısı çıkar; hedef **asla zayıf kliplerle doldurulmaz**.
- Render edilen klipler: `workspace/<proje-id>/clips/01_c01.mp4` …
- **Oyun → Tüm render edilmiş klipleri dışa aktar** → `exports/<YYYY-AA-GG>/<proje>_<klip>_<başlangıç>-<bitiş>.mp4`
  (Reels / TikTok / Shorts için 1080x1920, H.264 + AAC, +faststart). Yüklemeyi sen elle yaparsın.

Komut satırıyla render:

```bash
python cli.py render <id> 1 2 3 --layout split --cam-pos top --cam-ratio 0.30 --game-fit crop
python cli.py render <id> 1 --layout split --cam-pos bottom --game-fit blur --subs bold --sub-lang tr
```

## 6) Disk ve GPU rehberi

- Disk (ondalık GB): 4 Mbps'de 7 saat ≈ **12,6 GB**, 8 Mbps'de ≈ **25,2 GB** (klipler/önbellek hariç;
  gerçek bitrate değişir). Canlı panel boş alanı ve "kalan saat" tahminini gösterir.
  Düşük disk uyarısı 10 GB, kritik seviye 3 GB: kayan modda eski sabitlenmemiş parçalar silinir,
  arşiv modunda diski korumak için kayıt durur. Bekleyen aday/işaret/klip işlerinin parçaları asla silinmez.
- GPU (6 GB): Whisper large-v3-turbo float16 (~1,6 GB) ve qwen3.5:4b (~3,3 GB) **aynı anda GPU'da olmaz**;
  ağır işler tek sıralı GPU şeridinde çalışır. GPU hatasında Whisper CPU'ya düşer. Ollama yoksa/yavaşsa
  AI analizi atlanır; ses sinyalleri, manuel işaretler ve ffmpeg ile klip yine çıkar.
  Ayarlar → yerel AI'dan kapatılabilir.

## 7) Sorun giderme

```bash
source scripts/env.sh && cd backend
python cli.py doctor --no-fix            # ffmpeg, yt-dlp, Streamlink, Whisper, Ollama, GPU, NVENC, disk
python -m pip install -U "yt-dlp[default]"   # YouTube değişince ilk çare
nvidia-smi                               # GPU belleği
ollama list                              # yerel modeller
ls ../workspace/_live/                   # canlı oturumlar (segments/, segments.json, ffmpeg_run*.log)
python -m pytest -q backend/tests/test_local_gaming.py   # (repo kökünden) yerel testler
```

- "This stream needs a signed-in account" → üyelik/özel/yaş sınırı: desteklenmez (giriş yapılmaz).
- "This stream is not live right now" → VOD kartını kullan.
- Port meşgulse: `./scripts/start.sh --port 8001`.

## 8) Yeni (19.11.2026): hikâye klipleri, Instagram başlığı, yapay zekâ seçimi

- **Hikâye klipleri:** yapay zekâ tüm transkripti okur; her klip giriş -> doruk -> sonuç içerir, 45-120 sn.
  Uzunluklar: Oyun -> Klip yapısı (en kısa / hedef / en uzun). Aday sayısı aynı kartta.
- **Başlık:** her klibin üstünde, kamera ile oyunun birleştiği çizgide beyaz kutu + siyah yazı + emoji
  (ör. "Cops had zero chance 🚔"). Render panelindeki başlık kutusu boşsa her klip kendi başlığını kullanır.
- **Yapay zekâ seçimi (Oyun sayfası):** Yerel (ücretsiz, qwen3.5:4b) / Claude API / Gemini API.
  Claude: claude.ai -> Settings -> Billing -> API credits -> Link organization; console.anthropic.com'da anahtar
  oluştur, karta yapıştır -> Kaydet ve kullan -> Test et. Gemini: aistudio.google.com'dan ücretsiz anahtar.
  Gönderilen tek şey transkript metnidir; video/ses gitmez.
- **Facecam:** her klip kendi zaman aralığında yeniden kontrol edilir; kamera yer değiştirdiyse yeni yeri kullanılır.
  Elle çizdiğin kutu hiç değişmez. Düzen: üstte 1/3 yayıncı, altta 2/3 oyun.

## 9) İki mod, otomatik paylaşım, maliyet (19.11.2026)

- **Oyun sayfası -> Clip style:** *Streamer mode* (kamera üstte 1/3, oyun altta 2/3) veya *Direct mode*
  (görüntü analizi yok, 16:9 video ortada siyah zeminde, başlık hemen üstünde). Seçim tüm yeni içe aktarmalarda
  (Create clips sayfası dahil) kullanılır.
- **Uzunluk:** hedef 55 sn (Reels verisi: 45-60 sn en iyi), en kısa 30, en uzun 120; sadece çok güçlü hikâyeler 88 sn'yi geçer.
- **Auto-post kartı:** aç, platformları seç, günde kaç klip (en fazla 3), saatler. YouTube ve TikTok otomatik;
  Instagram hatırlatma (dosya + açıklama hazır, sen paylaşırsın). Bağlama adımları: `docs/PUBLISHING_SETUP.md`.
- **Yapay zekâ maliyeti:** `docs/AI_COSTS.md` (1 saatlik video: Claude Sonnet 5.5 ~0,65 $, Opus 5.5 ~1,3 $,
  Haiku 5.5 ~0,03 $, Gemini Flash ~0,25 $; Ultra planın 200 $/ay API kredisi bunu karşılar).
