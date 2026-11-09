# ============================================================
#   PC STUTTER TANI ARACI - Python versiyonu
#   Gereksinim: pip install psutil
#   Calistirmak icin: python stutter_tani.py
# ============================================================

import psutil
import time
import csv
import os
from datetime import datetime

LOG_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "stutter_log.csv")
INTERVAL = 0.8  # saniye

# --- Renk kodlari (Windows terminali destekliyorsa gorunur) ---
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
GRAY   = "\033[90m"
WHITE  = "\033[97m"

def renkli(metin, renk):
    return f"{renk}{metin}{RESET}"

def baslik():
    os.system("cls" if os.name == "nt" else "clear")
    print(renkli("  ╔══════════════════════════════════════════════════╗", CYAN))
    print(renkli("  ║      PC STUTTER TANI ARACI  v1.0  (Python)      ║", CYAN))
    print(renkli("  ║      Stutter yasadiginizda saati not alin!       ║", CYAN))
    print(renkli("  ╚══════════════════════════════════════════════════╝", CYAN))
    print()
    print(renkli(f"  Log dosyasi : {LOG_PATH}", YELLOW))
    print(renkli("  Durmak icin : CTRL+C", RED))
    print()
    print(renkli("  [YEŞİL]   = Normal", GREEN))
    print(renkli("  [SARI]    = Dikkat", YELLOW))
    print(renkli("  [KIRMIZI] = Sorun kaynagi olabilir, bu ani not alin!", RED))
    print()
    print(renkli("─" * 105, GRAY))
    print(renkli("  ZAMAN          CPU%   RAM Kullanim         Disk Oku    Disk Yaz    UYARILAR", CYAN))
    print(renkli("─" * 105, GRAY))

def sistem_verileri(onceki_disk):
    zaman = datetime.now().strftime("%H:%M:%S.%f")[:-4]

    # CPU
    cpu = psutil.cpu_percent(interval=None)

    # RAM
    ram = psutil.virtual_memory()
    ram_kullanilan = round(ram.used / (1024**3), 1)
    ram_toplam     = round(ram.total / (1024**3), 1)
    ram_pct        = ram.percent

    # Disk I/O (delta hesabi)
    disk_simdi = psutil.disk_io_counters()
    if onceki_disk:
        gecen_sure = INTERVAL
        oku_mb  = round((disk_simdi.read_bytes  - onceki_disk.read_bytes)  / (1024**2) / gecen_sure, 1)
        yaz_mb  = round((disk_simdi.write_bytes - onceki_disk.write_bytes) / (1024**2) / gecen_sure, 1)
    else:
        oku_mb, yaz_mb = 0.0, 0.0

    # Uyarilar
    uyarilar = []
    if cpu      > 88:  uyarilar.append(f"!CPU({cpu}%)")
    if ram_pct  > 88:  uyarilar.append(f"!RAM({ram_pct}%)")
    if oku_mb   > 150: uyarilar.append(f"!DISK_OKU({oku_mb}MB/s)")
    if yaz_mb   > 80:  uyarilar.append(f"!DISK_YAZ({yaz_mb}MB/s)")
    uyari_str = " | ".join(uyarilar)

    return {
        "zaman":          zaman,
        "cpu":            cpu,
        "ram_kullanilan": ram_kullanilan,
        "ram_toplam":     ram_toplam,
        "ram_pct":        ram_pct,
        "oku_mb":         oku_mb,
        "yaz_mb":         yaz_mb,
        "uyarilar":       uyari_str,
        "disk_obj":       disk_simdi,
    }

def renk_sec(v, uyari_var):
    if uyari_var:
        return RED
    if v["cpu"] > 70 or v["ram_pct"] > 75 or v["oku_mb"] > 80:
        return YELLOW
    return GREEN

def main():
    # psutil kontrol
    try:
        import psutil
    except ImportError:
        print(RED + "\n  [HATA] psutil yuklu degil!" + RESET)
        print("  Lutfen su komutu calistirin: pip install psutil")
        input("\n  Cikmak icin Enter'a basin...")
        return

    # Windows'ta ANSI renk aktif et
    if os.name == "nt":
        os.system("color")

    baslik()

    # CSV olustur
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Zaman", "CPU_Pct", "RAM_GB", "RAM_Toplam_GB",
                         "RAM_Pct", "Disk_Oku_MBs", "Disk_Yaz_MBs", "Uyarilar"])

    # Ilk CPU olcumu baslat (non-blocking)
    psutil.cpu_percent(interval=None)
    onceki_disk = psutil.disk_io_counters()

    try:
        while True:
            time.sleep(INTERVAL)
            v = sistem_verileri(onceki_disk)
            onceki_disk = v["disk_obj"]

            renk = renk_sec(v, bool(v["uyarilar"]))

            satir = (
                f"  {v['zaman']}   "
                f"CPU: {v['cpu']:5.1f}%   "
                f"RAM: {v['ram_kullanilan']:4.1f}/{v['ram_toplam']:4.1f} GB ({v['ram_pct']:3.0f}%)   "
                f"Disk: {v['oku_mb']:6.1f} MB/s oku   "
                f"{v['yaz_mb']:6.1f} MB/s yaz"
            )
            if v["uyarilar"]:
                satir += f"   ⚠  {v['uyarilar']}"

            print(renkli(satir, renk))

            # CSV'ye ekle
            with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    v["zaman"], v["cpu"], v["ram_kullanilan"],
                    v["ram_toplam"], v["ram_pct"],
                    v["oku_mb"], v["yaz_mb"], v["uyarilar"]
                ])

    except KeyboardInterrupt:
        print()
        print(renkli("  ✓ Durduruldu. Log dosyasi:", GREEN), LOG_PATH)
        print()

if __name__ == "__main__":
    main()
