# ============================================================
#   PC TAM TEMİZLİK ARACI
#   1) Önce tarar ve rapor çıkarır
#   2) Onay verdikten sonra siler
#   Gereksinim: pip install psutil
#   Çalıştır: python pc_temizlik.py
# ============================================================

import os
import shutil
import time
import sys
from pathlib import Path
from datetime import datetime

# ── Renkler ──────────────────────────────────────────────────
R = "\033[91m"; G = "\033[92m"; Y = "\033[93m"
C = "\033[96m"; W = "\033[97m"; D = "\033[90m"; X = "\033[0m"

def c(t, r): return f"{r}{t}{X}"

# ── Boyut formatla ────────────────────────────────────────────
def boyut(b):
    for u in ["B","KB","MB","GB","TB"]:
        if abs(b) < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"

# ── Klasör boyutu hesapla ─────────────────────────────────────
def klasor_boyut(yol):
    toplam = 0
    try:
        for dirpath, _, filenames in os.walk(yol):
            for f in filenames:
                try:
                    fp = os.path.join(dirpath, f)
                    toplam += os.path.getsize(fp)
                except: pass
    except: pass
    return toplam

# ── Güvenli silinebilecek konumlar ───────────────────────────
KULLANICI = os.path.expanduser("~")

GUVENLI_CONUMLAR = [
    # Windows Temp
    (r"C:\Windows\Temp",                    "Windows Geçici Dosyalar"),
    (r"C:\Windows\SoftwareDistribution\Download", "Windows Update Önbelleği"),
    (r"C:\Windows\Prefetch",                "Prefetch Dosyaları"),
    # Kullanıcı Temp
    (os.path.join(KULLANICI, "AppData", "Local", "Temp"), "Kullanıcı Geçici Dosyalar"),
    # Tarayıcı cache
    (os.path.join(KULLANICI, "AppData", "Local", "Opera Software", "Opera GX Stable", "Cache"), "Opera GX Önbelleği"),
    (os.path.join(KULLANICI, "AppData", "Local", "Google", "Chrome", "User Data", "Default", "Cache"), "Chrome Önbelleği"),
    (os.path.join(KULLANICI, "AppData", "Local", "Microsoft", "Edge", "User Data", "Default", "Cache"), "Edge Önbelleği"),
    # Windows hata raporları
    (os.path.join(KULLANICI, "AppData", "Local", "CrashDumps"),  "Kilitlenme Dökümleri"),
    (r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive",      "Windows Hata Raporları"),
    # Thumbnail cache
    (os.path.join(KULLANICI, "AppData", "Local", "Microsoft", "Windows", "Explorer"), "Küçük Resim Önbelleği"),
    # Discord cache
    (os.path.join(KULLANICI, "AppData", "Roaming", "discord", "Cache"), "Discord Önbelleği"),
    (os.path.join(KULLANICI, "AppData", "Roaming", "discord", "Code Cache"), "Discord Kod Önbelleği"),
    # Spotify cache
    (os.path.join(KULLANICI, "AppData", "Local", "Spotify", "Data"), "Spotify Önbelleği"),
    # NVIDIA shader cache
    (os.path.join(KULLANICI, "AppData", "Local", "NVIDIA", "DXCache"), "NVIDIA DX Shader Önbelleği"),
    (os.path.join(KULLANICI, "AppData", "Local", "NVIDIA", "GLCache"), "NVIDIA GL Shader Önbelleği"),
    # Windows.old
    (r"C:\Windows.old", "Eski Windows Kurulumu (ÇOK BÜYÜK OLABİLİR)"),
]

# ── İncelenmesi önerilen büyük klasörler ─────────────────────
INCELEME_KONUMLARI = [
    (os.path.join(KULLANICI, "Downloads"),   "İndirilenler"),
    (os.path.join(KULLANICI, "Desktop"),     "Masaüstü"),
    (os.path.join(KULLANICI, "Videos"),      "Videolar"),
    (os.path.join(KULLANICI, "AppData", "Local", "Packages"), "UWP Uygulama Verileri"),
    (r"C:\ProgramData",                      "Program Verileri"),
]

# ── Büyük dosya tarama (MB limitinin üstü) ───────────────────
BUYUK_DOSYA_MB = 500  # Bu MB'dan büyük dosyaları listele

# ── Kesinlikle dokunulmayacak yerler ─────────────────────────
ATLA = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\Windows\System32",
    r"C:\Windows\SysWOW64",
    r"C:\Windows\WinSxS",
    # Steam — kesinlikle dokunma
    "Steam",
    "SteamLibrary",
    "steamapps",
    "steam",
    # Python — her versiyonu koru
    "python",
    "Python3",
    "Python38","Python39","Python310","Python311","Python312","Python313",
    "site-packages",
    "Lib\\site-packages",
    # VS Code — koru
    "Visual Studio Code",
    "vscode",
    "Code - OSS",
    # Geliştirme araçları
    "nodejs", "node_modules",
    "git", ".git",
    "GitHub",
]

def atlanacak_mi(yol):
    yol_str = str(yol).lower()
    for a in ATLA:
        if a.lower() in yol_str:
            return True
    return False

# ── Pip paket analizi ─────────────────────────────────────────
import subprocess
import glob

def pip_analizi(yaz):
    yaz("  ┌─────────────────────────────────────────────────┐", Y)
    yaz("  │  5. PIP PAKETLERİ & VS CODE ANALİZİ            │", Y)
    yaz("  └─────────────────────────────────────────────────┘\n", Y)

    # ── VS Code boyutu ────────────────────────────────────────
    vscode_yollari = [
        os.path.join(KULLANICI, "AppData", "Local", "Programs", "Microsoft VS Code"),
        r"C:\Program Files\Microsoft VS Code",
    ]
    for yol in vscode_yollari:
        if os.path.exists(yol):
            b = klasor_boyut(yol)
            yaz(f"  VS Code (program kendisi)       {boyut(b):>10}", G)
            break

    # VS Code extensions
    ext_yol = os.path.join(KULLANICI, ".vscode", "extensions")
    if os.path.exists(ext_yol):
        b = klasor_boyut(ext_yol)
        renk = R if b > 2*1024**3 else Y if b > 500*1024**2 else G
        yaz(f"  VS Code Extensions klasörü      {boyut(b):>10}  ← Kullanmadığın eklentiler burada", renk)

        # En büyük extensionları listele
        try:
            ext_listesi = []
            for item in Path(ext_yol).iterdir():
                if item.is_dir():
                    ib = klasor_boyut(str(item))
                    ext_listesi.append((ib, item.name))
            ext_listesi.sort(reverse=True)
            yaz(f"\n  En büyük VS Code eklentileri (ilk 10):", D)
            for ib, isim in ext_listesi[:10]:
                renk = R if ib > 200*1024**2 else Y if ib > 50*1024**2 else G
                yaz(f"    {boyut(ib):>10}  {isim}", renk)
        except: pass

    yaz("")

    # ── Python site-packages ──────────────────────────────────
    python_yollari = glob.glob(r"C:\Users\*\AppData\Local\Programs\Python\Python*\Lib\site-packages") + \
                     glob.glob(r"C:\Python*\Lib\site-packages") + \
                     glob.glob(r"C:\Program Files\Python*\Lib\site-packages")

    if not python_yollari:
        # sys.path üzerinden bul
        import site
        try:
            python_yollari = [p for p in site.getsitepackages() if os.path.exists(p)]
        except: pass

    if python_yollari:
        yaz(f"  Python site-packages konumları:", D)
        toplam_pip = 0
        for sp in python_yollari:
            b = klasor_boyut(sp)
            toplam_pip += b
            renk = R if b > 3*1024**3 else Y if b > 500*1024**2 else G
            yaz(f"  {boyut(b):>10}  {sp}", renk)

        yaz(f"\n  Toplam pip paket boyutu: {c(boyut(toplam_pip), Y)}", W)
        yaz(f"  (Sadece bilgi — pip paketleri otomatik silinmez)\n", D)

        # En büyük paketleri listele
        try:
            # pip list ile paket listesini al
            sonuc = subprocess.run(
                ["pip", "list", "--format=columns"],
                capture_output=True, text=True, timeout=15
            )
            if sonuc.returncode == 0:
                yaz(f"  Yüklü pip paketleri ({len(sonuc.stdout.strip().splitlines())-2} adet):", D)

                # site-packages içindeki klasörleri boyuta göre sırala
                paket_boyutlari = []
                for sp in python_yollari:
                    if not os.path.exists(sp): continue
                    for item in Path(sp).iterdir():
                        if item.is_dir() and not item.name.endswith(".dist-info") and not item.name.endswith(".data"):
                            ib = klasor_boyut(str(item))
                            if ib > 10 * 1024 * 1024:  # 10MB üstü göster
                                paket_boyutlari.append((ib, item.name))

                paket_boyutlari.sort(reverse=True)
                yaz(f"\n  En büyük pip paketleri (10MB+):", D)
                if paket_boyutlari:
                    for ib, isim in paket_boyutlari[:20]:
                        renk = R if ib > 1*1024**3 else Y if ib > 200*1024**2 else G
                        yaz(f"    {boyut(ib):>10}  {isim}", renk)
                    yaz(f"\n  → Silmek için: pip uninstall <paket_adi>", D)
                else:
                    yaz(f"    Büyük paket bulunamadı (temiz görünüyor!)", G)
        except Exception as e:
            yaz(f"  pip listesi alınamadı: {e}", D)
    else:
        yaz("  Python site-packages bulunamadı.", D)

    # ── Venv taraması ─────────────────────────────────────────
    yaz(f"\n  Sanal ortam (venv) taraması...", D)
    venv_listesi = []
    arama_yollari = [
        KULLANICI,
        os.path.join(KULLANICI, "Documents"),
        os.path.join(KULLANICI, "Desktop"),
        os.path.join(KULLANICI, "Projects"),
        os.path.join(KULLANICI, "dev"),
        os.path.join(KULLANICI, "repos"),
    ]
    for arama in arama_yollari:
        if not os.path.exists(arama): continue
        try:
            for dirpath, dirs, files in os.walk(arama):
                # venv işaretlerini ara
                if "pyvenv.cfg" in files:
                    b = klasor_boyut(dirpath)
                    venv_listesi.append((b, dirpath))
                    dirs.clear()  # içine girme
                # node_modules gibi ağır klasörleri atla
                dirs[:] = [d for d in dirs if d not in [".git", "__pycache__", "node_modules"]]
        except: pass

    if venv_listesi:
        venv_listesi.sort(reverse=True)
        toplam_venv = sum(b for b, _ in venv_listesi)
        yaz(f"  {len(venv_listesi)} adet venv bulundu — toplam: {c(boyut(toplam_venv), Y)}", Y)
        for b, yol in venv_listesi[:10]:
            renk = R if b > 500*1024**2 else Y
            yaz(f"    {boyut(b):>10}  {yol}", renk)
        yaz(f"\n  → Kullanmadığın venv klasörlerini elle silebilirsin.", D)
    else:
        yaz("  Venv bulunamadı.", G)

    yaz("")

# ═══════════════════════════════════════════════════════════════
def baslik():
    os.system("cls" if os.name == "nt" else "clear")
    if os.name == "nt": os.system("color")
    print(c("\n  ╔══════════════════════════════════════════════════╗", C))
    print(c("  ║        PC TAM TEMİZLİK ARACI  v3.0              ║", C))
    print(c("  ╚══════════════════════════════════════════════════╝\n", C))

# ═══════════════════════════════════════════════════════════════
def tarama():
    rapor_satirlari = []
    silinecekler    = []  # (yol, etiket, boyut)
    toplam_kazanim  = 0

    log_yol = os.path.join(KULLANICI, "Desktop", "temizlik_raporu.txt")

    def yaz(metin="", renk=W, log=True):
        print(c(metin, renk))
        if log: rapor_satirlari.append(metin)

    baslik()
    yaz(f"  Tarama başladı: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}", C)
    yaz(f"  Rapor: {log_yol}\n", D)

    # ── 1) Güvenli silinebilecekler ──────────────────────────
    yaz("  ┌─────────────────────────────────────────────────┐", Y)
    yaz("  │  1. GÜVENLİ SİLİNEBİLECEK KONUMLAR             │", Y)
    yaz("  └─────────────────────────────────────────────────┘\n", Y)

    for yol, etiket in GUVENLI_CONUMLAR:
        if not os.path.exists(yol):
            yaz(f"  [ -- ] {etiket:<40} (bulunamadı)", D, log=False)
            continue
        b = klasor_boyut(yol)
        toplam_kazanim += b
        silinecekler.append((yol, etiket, b))
        renk = R if b > 500*1024*1024 else Y if b > 50*1024*1024 else G
        yaz(f"  [SİL] {etiket:<42} {boyut(b):>10}", renk)

    yaz(f"\n  → Güvenli temizlikten kazanılacak: {c(boyut(toplam_kazanim), R)}\n")

    # ── 2) İnceleme önerilen klasörler ───────────────────────
    yaz("  ┌─────────────────────────────────────────────────┐", Y)
    yaz("  │  2. İNCELEMENİ ÖNERDİĞİM KLASÖRLER             │", Y)
    yaz("  └─────────────────────────────────────────────────┘\n", Y)

    for yol, etiket in INCELEME_KONUMLARI:
        if not os.path.exists(yol): continue
        print(f"  Tarıyor: {etiket}...", end="\r")
        b = klasor_boyut(yol)
        renk = R if b > 5*1024**3 else Y if b > 1*1024**3 else G
        yaz(f"  [BAK]  {etiket:<42} {boyut(b):>10}", renk)

    yaz("")

    # ── 3) Büyük dosya taraması ───────────────────────────────
    yaz("  ┌─────────────────────────────────────────────────┐", Y)
    yaz(f"  │  3. {BUYUK_DOSYA_MB}MB'DAN BÜYÜK DOSYALAR (İLK 100)        │", Y)
    yaz("  └─────────────────────────────────────────────────┘\n", Y)

    yaz("  Taranıyor (bu biraz sürebilir)...", D)
    buyuk_dosyalar = []
    limit = BUYUK_DOSYA_MB * 1024 * 1024

    try:
        for dirpath, dirs, files in os.walk("C:\\"):
            # Atlanacak klasörleri kırp
            dirs[:] = [d for d in dirs if not atlanacak_mi(os.path.join(dirpath, d))]
            for dosya in files:
                try:
                    fp = os.path.join(dirpath, dosya)
                    b  = os.path.getsize(fp)
                    if b >= limit:
                        buyuk_dosyalar.append((b, fp))
                except: pass
    except: pass

    buyuk_dosyalar.sort(reverse=True)
    for b, fp in buyuk_dosyalar[:100]:
        uzanti = Path(fp).suffix.lower()
        renk = R if uzanti in [".iso",".zip",".rar",".7z",".bak",".old",".tmp"] else Y
        yaz(f"  {boyut(b):>10}  {fp}", renk)

    if not buyuk_dosyalar:
        yaz("  Büyük dosya bulunamadı (zaten temiz).", G)

    # ── 5) Pip & VS Code analizi ─────────────────────────────
    pip_analizi(yaz)

    # ── 4) Uninstall önerileri ────────────────────────────────
    yaz("\n  ┌─────────────────────────────────────────────────┐", Y)
    yaz("  │  4. KALDIRILMASINI ÖNERDİĞİM PROGRAMLAR        │", Y)
    yaz("  └─────────────────────────────────────────────────┘\n", Y)

    oneriler = [
        ("McAfee",       "Windows Defender zaten var, McAfee hem RAM hem oyun performansını etkiliyor"),
        ("McAfee",       "Kaldırma için: mccleanup.exe aracını kullan (Google'da ara)"),
        ("Wallpaper Engine oyun sırasında", "Sistem tepsisinden 'Duraklat' seçeneğini kullan"),
        ("Phone Link",   "Telefonu PC'ye bağlamak için kullanmıyorsan kaldırabilirsin"),
        ("Microsoft Teams", "İş için kullanmıyorsan kaldır, arka planda RAM yiyor"),
        ("Xbox Game Bar", "Oyun kaydı için kullanmıyorsan: Ayarlar → Oyun → Xbox Game Bar → Kapat"),
    ]

    for program, neden in oneriler:
        yaz(f"  ► {program}", R)
        yaz(f"    {neden}\n", D)

    # ── Raporu kaydet ─────────────────────────────────────────
    with open(log_yol, "w", encoding="utf-8") as f:
        f.write(f"PC TEMİZLİK RAPORU — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n")
        f.write("="*60 + "\n\n")
        for satir in rapor_satirlari:
            f.write(satir.strip() + "\n")

    yaz(f"\n  Rapor masaüstüne kaydedildi: temizlik_raporu.txt", G)

    return silinecekler, toplam_kazanim

# ═══════════════════════════════════════════════════════════════
def temizle(silinecekler):
    print(c("\n  ╔══════════════════════════════════════════════════╗", R))
    print(c("  ║              TEMİZLEME BAŞLIYOR                 ║", R))
    print(c("  ╚══════════════════════════════════════════════════╝\n", R))

    toplam_silinen = 0

    for yol, etiket, _ in silinecekler:
        if not os.path.exists(yol):
            continue
        print(f"  Temizleniyor: {etiket}...", end=" ")
        try:
            onceki = klasor_boyut(yol)
            # Klasör içindekileri sil ama klasörü bırak
            for item in Path(yol).iterdir():
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                except: pass
            toplam_silinen += onceki
            print(c(f"✓  ({boyut(onceki)} silindi)", G))
        except Exception as e:
            print(c(f"✗  ({e})", R))

    print(c(f"\n  ✓ Toplam temizlenen: {boyut(toplam_silinen)}", G))
    print(c("  Bilgisayarını yeniden başlatmanı öneririm.\n", Y))

# ═══════════════════════════════════════════════════════════════
def main():
    if os.name != "nt":
        print("Bu script sadece Windows'ta çalışır.")
        sys.exit(1)

    silinecekler, toplam = tarama()

    print(c(f"\n  {'─'*50}", D))
    print(c(f"  Güvenli temizlikten tahmini kazanım: {boyut(toplam)}", Y))
    print(c(f"  {'─'*50}\n", D))

    print(c("  Ne yapmak istersin?", W))
    print(c("  [1] Güvenli dosyaları sil (önerilen)", G))
    print(c("  [2] Sadece raporu gör, silme", Y))
    print(c("  [3] Çık\n", D))

    secim = input(c("  Seçimin (1/2/3): ", C)).strip()

    if secim == "1":
        print(c("\n  UYARI: Bu işlem geri alınamaz!", R))
        onay = input(c("  Devam etmek için 'EVET' yaz: ", Y)).strip()
        if onay.upper() == "EVET":
            temizle(silinecekler)
        else:
            print(c("\n  İptal edildi.", Y))
    elif secim == "2":
        print(c("\n  Rapor masaüstünde: temizlik_raporu.txt", G))
    else:
        print(c("\n  Çıkıldı.", D))

    input(c("\n  Çıkmak için Enter'a bas...", D))

if __name__ == "__main__":
    main()
