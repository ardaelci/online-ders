# -*- coding: utf-8 -*-
"""
Biruni BEM (Moodle + BigBlueButton) canli derse otomatik katilir.
Mikrofon ASLA acilmaz: her zaman "Yalnizca dinleme" ile katilir.

Akis (tamamen otomatik):
 1) Ilk calistirmada kullanici adi, sifre ve ders linki sorulur (bir daha sorulmaz).
 2) Okula otomatik giris yapar, derse girer, "Ilerle" ve "Yalnizca dinleme"yi secer.
 3) Ders kapanirsa 5 dk'da bir girene kadar tekrar dener.
 4) Program, ACILDIKTAN 6 saat sonra kendini ve BILGISAYARI kapatir
    (60 sn uyari verir; iptal: Win+R -> shutdown /a).

Saglamlik:
 - Internet giderse cokmez, tekrar dener.
 - Tarayici kapanir/coker ise kendiliginden yeniden acar.
 - Kayitli sifre yanlissa en fazla 2 kez dener (okul hesabi kilitlenmesin).
 - Bilgisayarin ayarlarini DEGISTIRMEZ; sadece program acikken uykuyu engeller.
 - Ayni anda iki kez acilmasini engeller.
 - Her seyi 'kayit.txt' dosyasina yazar.
"""
import base64
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError

# ==================== AYARLAR ====================
TOPLAM_SURE       = 6 * 60 * 60   # program acildiktan kac sn sonra kapansin (6 saat)
TEKRAR_ARALIK     = 5 * 60        # ders kapaninca kac sn'de bir tekrar denesin (5 dk)
ILK_DENEME_ARALIK = 30            # henuz hic girilmediyse kac sn'de bir denesin
BILGISAYARI_KAPAT = True          # sure dolunca bilgisayari da kapat (False = kapatma)
KAPATMA_UYARI     = 60            # kapatmadan once kac sn uyari versin
# =================================================

KONTROL_ARALIK   = 10    # ders sekmesi kac sn'de bir kontrol edilsin
KATIL_BEKLEME    = 45    # linke gidince ders ekraninin acilmasi icin en fazla bekleme (sn)
SES_BEKLEME      = 90    # ses secim penceresi icin en fazla bekleme (sn)
YENIDEN_AC_BEKLE = 15    # tarayici kapanirsa kac sn sonra yeniden acilsin
TEK_KOPYA_PORT   = 47813 # ayni anda iki kopya calismasin diye
HEADLESS         = os.environ.get("DK_HEADLESS") == "1"
KANCA            = None  # (test icin) tarayici acilinca cagrilir

KLASOR      = Path(os.environ.get("DK_KLASOR") or Path(__file__).resolve().parent)
PROFIL      = KLASOR / "profil"
GIRIS_DOSYA = KLASOR / "giris.dat"
KAYIT_DOSYA = KLASOR / "kayit.txt"

# Calisma sirasinda doldurulur
LINK = ""
KULLANICI = None
SIFRE = None
BITIS = 0.0
OTO_GIRIS_BOZUK = False          # kayitli sifre tutmadiysa tekrar denenmez
DURUM = {"kapandi": False}       # tarayici kapandi mi


# ---------- Ekranda aranan yazilar (hepsi kucuk harf) ----------
KAYIT_UYARISI = ["kayıt alınıyor", "kayit aliniyor", "kaydı kullanılabilecek",
                 "kaydi kullanilabilecek", "being recorded", "is being recorded"]
SES_MODAL = ["nasıl katılmak istersiniz", "nasil katilmak istersiniz",
             "sesli toplantıya", "sesli toplantiya", "how would you like to join"]
AKTIVITE = ["etkinlik kontrol", "aktivite kontrol", "hala toplantıda", "hâlâ toplantıda",
            "hala toplantida", "still in meeting", "activity check", "hala orada mısınız",
            "are you still there"]
BITTI = ["oturum sona erdi", "toplantı sona erdi", "toplanti sona erdi",
         "oturum kapatıldı", "oturum kapatildi", "ders sona erdi", "oturum sonlandırıldı",
         "the meeting has ended", "this session has ended", "meeting has ended",
         "session has ended", "you have been logged out", "oturumdan çıkış yaptınız"]

# ---------- Tiklanacak butonlar ----------
OTURUMA_KATIL  = ["Oturuma katıl", "Oturuma katil", "Join session", "Katıl", "Katil", "Join"]
ILERLE         = ["İlerle", "Ilerle", "Devam et", "Continue"]
SES_DINLE      = ["Yalnızca dinleme", "Yalnizca dinleme", "Yalnızca dinle", "Yalnizca dinle",
                  "Yalnız dinle", "Yalniz dinle", "Dinleyici olarak katıl",
                  "Dinleyici olarak katil", "Dinleyici", "Listen only", "Dinle"]
AKTIVITE_BUTON = ["Kontrol et", "Denetle", "Onayla", "Buradayım", "Hâlâ buradayım",
                  "Hala buradayım", "Check", "I'm here", "I'm still here", "Evet"]
MIKROFON_YASAK = ["mikrofon", "microphone", "konuş", "konus", "speak"]


class TarayiciKapandi(Exception):
    pass


# ======================= Yardimcilar =======================
def log(*a):
    satir = "[" + time.strftime("%H:%M:%S") + "] " + " ".join(str(x) for x in a)
    try:
        print(satir, flush=True)
    except Exception:
        try:
            print(satir.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    try:
        with open(KAYIT_DOSYA, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + satir + "\n")
    except Exception:
        pass


def kisa(e):
    t = str(e).strip().splitlines()
    return (t[0] if t else repr(e))[:160]


def kayit_kirp():
    try:
        if KAYIT_DOSYA.exists() and KAYIT_DOSYA.stat().st_size > 2_000_000:
            KAYIT_DOSYA.write_bytes(KAYIT_DOSYA.read_bytes()[-500_000:])
    except Exception:
        pass


def bip(kez=3):
    try:
        import winsound
        for _ in range(kez):
            winsound.Beep(1100, 300)
    except Exception:
        pass


def bekle(sn):
    """Uyur; bu sirada tarayici kapanirsa hemen haber verir."""
    son = time.time() + sn
    while time.time() < son:
        if DURUM["kapandi"]:
            raise TarayiciKapandi()
        time.sleep(min(0.5, max(0.0, son - time.time())))


def _kapandiysa_firlat():
    if DURUM["kapandi"]:
        raise TarayiciKapandi()


def normal(t):
    return (t or "").replace("İ", "i").lower()


# ======================= Uyku (Windows) =======================
# Ayarlari DEGISTIRMEZ. Windows'a "bu program calisirken uyuma" der.
# Program kapaninca (cokse bile) Windows bunu kendiliginden geri alir.
def uyku_engelle():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            log("Program acikken bilgisayarin uykuya gecmesi engellendi (ayarlarin degismedi).")
    except Exception as e:
        log("Uyku engellenemedi:", kisa(e))


def uyku_serbest():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass


def bilgisayari_kapat():
    """Windows'u KAPATMA_UYARI sn sonra kapatir. Iptal: Win+R -> shutdown /a"""
    if sys.platform != "win32":
        log("(Windows degil, bilgisayar kapatilmadi.)")
        return
    try:
        import subprocess
        subprocess.run(["shutdown", "/s", "/t", str(KAPATMA_UYARI), "/c",
                        "Ders programi bitti. Bilgisayar " + str(KAPATMA_UYARI) +
                        " sn sonra kapanacak. Iptal: Win+R -> shutdown /a"],
                       timeout=20)
        log("Bilgisayar " + str(KAPATMA_UYARI) + " sn sonra kapanacak.")
        log("IPTAL ETMEK ICIN: Win+R tuslarina bas, 'shutdown /a' yaz, Enter.")
    except Exception as e:
        log("Bilgisayar kapatilamadi:", kisa(e))


# ======================= Tek kopya =======================
def tek_kopya_kilidi():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", TEK_KOPYA_PORT))
        return s
    except OSError:
        s.close()
        return None


# ======================= Giris bilgileri =======================
# base64 ile karistirilir: gercek sifreleme degil, dosyayi acan biri ciplak
# gozle okuyamasin diye.
def _karistir(metin):
    return base64.b64encode(metin.encode("utf-8")).decode("ascii")


def _coz(veri):
    try:
        return base64.b64decode(veri.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def giris_bilgisi_oku():
    if not GIRIS_DOSYA.exists():
        return None, None, None
    try:
        d = json.loads(GIRIS_DOSYA.read_text(encoding="utf-8"))
        return _coz(d.get("k", "")), _coz(d.get("s", "")), _coz(d.get("l", ""))
    except Exception:
        return None, None, None


def _link_gecerli_mi(link):
    l = link.lower()
    return l.startswith("http") and "biruni.edu.tr" in l and "bigbluebuttonbn" in l


def giris_bilgisi_sor_ve_kaydet():
    print("\n=== ILK KURULUM (bu bilgiler SADECE ILK SEFER sorulur) ===")
    print("Bu klasordeki 'giris.dat' dosyasina kaydedilir. Degistirmek icin o dosyayi sil.")
    print("NOT: Yazdiklarin ekranda GORUNUR.\n")
    kullanici = ""
    while not kullanici:
        kullanici = input("Okul kullanici adin: ").strip()
    sifre = ""
    while not sifre:
        sifre = input("Okul sifren: ").strip()
    print("\nDers linki: okul sisteminde canli derse sag tikla ->")
    print("'Baglanti adresini kopyala', sonra buraya yapistir.")
    while True:
        link = input("Ders linki: ").strip().strip('"').strip("'").strip()
        if not link:
            continue
        if _link_gecerli_mi(link):
            break
        print(">>> Bu link Biruni canli ders linki gibi gorunmuyor.")
        if input("    Yine de kullanmak icin 'e' yaz, degilse Enter: ").strip().lower() in ("e", "evet"):
            break
    try:
        GIRIS_DOSYA.write_text(json.dumps({"k": _karistir(kullanici), "s": _karistir(sifre),
                                           "l": _karistir(link)}), encoding="utf-8")
        log("Bilgiler kaydedildi (giris.dat).")
    except Exception as e:
        log("Bilgiler kaydedilemedi (bu seferlik kullanilacak):", kisa(e))
    return kullanici, sifre, link


# ======================= Sayfa yardimcilari =======================
def sayfa_metni(s):
    try:
        return normal(s.inner_text("body", timeout=3000))
    except Exception:
        _kapandiysa_firlat()
        return ""


def git(s, url):
    try:
        s.goto(url, wait_until="domcontentloaded", timeout=30000)
        bekle(2)   # yonlendirmeler tamamlansin
        return True
    except TarayiciKapandi:
        raise
    except Exception as e:
        _kapandiysa_firlat()
        log("Sayfa acilamadi (internet olabilir):", kisa(e))
        return False


def tikla(s, yazilar, yasak=(), timeout=1500):
    """Gorunur buton/link/yaziyi bulup tiklar. Once BIREBIR eslesme arar;
    bulamazsa uzun yazilar icin icinde gecen eslesmeyi dener. 'yasak'
    kelimelerden birini iceren elemana asla tiklamaz."""
    for gevsek in (False, True):
        for yazi in yazilar:
            if gevsek and len(yazi) <= 6:
                continue  # kisa kelimeler (Katil, Dinle...) sadece birebir
            if gevsek:
                desen = re.compile(re.escape(yazi), re.I)
            else:
                desen = re.compile(r"^\s*" + re.escape(yazi) + r"\s*$", re.I)
            for bul in (lambda: s.get_by_role("button", name=desen),
                        lambda: s.get_by_role("link", name=desen),
                        lambda: s.get_by_text(desen)):
                try:
                    adaylar = bul().all()
                except Exception:
                    _kapandiysa_firlat()
                    continue
                for h in adaylar[:5]:
                    try:
                        if not h.is_visible():
                            continue
                        if yasak:
                            etiket = normal(h.inner_text(timeout=800)) + " " + \
                                     normal(h.get_attribute("aria-label", timeout=800))
                            if any(k in etiket for k in yasak):
                                continue
                        h.click(timeout=timeout)
                        return yazi
                    except Exception:
                        _kapandiysa_firlat()
                        continue
    return None


def ders_url_mi(url):
    return "html5client" in (url or "").lower()


def giris_sayfasi_mi(s):
    try:
        u = s.url.lower()
    except Exception:
        _kapandiysa_firlat()
        return False
    if ders_url_mi(u):
        return False
    if "/login/" in u or "wantsurl" in u:
        return True
    try:
        sifre_kutusu = s.locator("input[type='password']").count() > 0
    except Exception:
        _kapandiysa_firlat()
        sifre_kutusu = False
    if not sifre_kutusu:
        return False
    m = sayfa_metni(s)
    return any(k in m for k in ("giriş yap", "giris yap", "kullanıcı adı",
                                "kullanici adi", "log in", "username"))


def ders_bitti_mi(s):
    """Ders bitis ekrani mi? SOHBET MESAJLARINA BAKMAZ: sadece BigBlueButton'in
    bitis penceresine, sayfa basliklarina ve cok kisa sayfalara bakar."""
    for sec in ('[data-test="meetingEndedModal"]', '[data-test="meetingEndedModalTitle"]'):
        try:
            if s.locator(sec).count() > 0:
                return True
        except Exception:
            _kapandiysa_firlat()
    try:
        basliklar = s.locator("h1, h2, h3, [role='heading']").all_inner_texts()
    except Exception:
        _kapandiysa_firlat()
        basliklar = []
    for b in basliklar:
        if any(k in normal(b) for k in BITTI):
            return True
    # Bitis ekrani cok kisa bir sayfadir; canli ders sayfasi asla bu kadar kisa olmaz
    m = sayfa_metni(s)
    return bool(m) and len(m) < 400 and any(k in m for k in BITTI)


def sekme_kapat(context, s):
    try:
        if len(context.pages) <= 1:
            context.new_page()   # son sekme kapanirsa tarayici kapanmasin
        s.close()
    except Exception:
        _kapandiysa_firlat()


def ders_sekmesi(context):
    """Canli ders (BigBlueButton) sekmesi varsa dondurur. Bitmis olanlari kapatir."""
    for s in list(context.pages):
        try:
            if s.is_closed() or not ders_url_mi(s.url):
                continue
        except Exception:
            _kapandiysa_firlat()
            continue
        if ders_bitti_mi(s):
            log("Ders bitmis (bitis ekrani goruldu), sekme kapatiliyor.")
            sekme_kapat(context, s)
            continue
        return s
    return None


def calisma_sekmesi(context):
    for s in list(context.pages):
        try:
            if not s.is_closed() and not ders_url_mi(s.url):
                return s
        except Exception:
            _kapandiysa_firlat()
    return context.new_page()


def fazla_sekmeleri_kapat(context, tut):
    for s in list(context.pages):
        if s is not tut:
            try:
                s.close()
            except Exception:
                _kapandiysa_firlat()


def ekran_goruntusu(s):
    try:
        s.screenshot(path=str(KLASOR / "son_katilim.png"))
    except Exception:
        pass


# ======================= Giris =======================
def formu_doldur(s):
    try:
        kadi = sif = None
        for sec in ("#username", "input[name='username']", "input[autocomplete='username']"):
            if s.locator(sec).count() > 0:
                kadi = s.locator(sec).first
                break
        for sec in ("#password", "input[name='password']", "input[type='password']"):
            if s.locator(sec).count() > 0:
                sif = s.locator(sec).first
                break
        if not kadi or not sif:
            return False
        kadi.fill(KULLANICI, timeout=5000)
        sif.fill(SIFRE, timeout=5000)
        if s.locator("#loginbtn").count() > 0:
            s.locator("#loginbtn").first.click(timeout=5000)
        elif not tikla(s, ["Giriş yap", "Giris yap", "Log in"]):
            sif.press("Enter")
        return True
    except Exception as e:
        _kapandiysa_firlat()
        log("Giris formu doldurulamadi:", kisa(e))
        return False


def giris_yap(s):
    """Giris sayfasindayken cagrilir. Giris olursa True."""
    global OTO_GIRIS_BOZUK
    if KULLANICI and SIFRE and not OTO_GIRIS_BOZUK:
        for _ in range(2):
            if not formu_doldur(s):
                bekle(3)
                if not giris_sayfasi_mi(s):
                    log("Okula giris basarili.")
                    return True
                continue
            log("Otomatik giris yapiliyor...")
            # Sayfa yavas acilabilir: 20 sn'ye kadar giris sayfasindan cikmasini bekle
            son = time.time() + 20
            while time.time() < son:
                bekle(2)
                if not giris_sayfasi_mi(s):
                    log("Okula giris basarili.")
                    return True
                if "geçersiz" in sayfa_metni(s) or "invalid login" in sayfa_metni(s):
                    break   # sifre yanlis, bosuna bekleme
        OTO_GIRIS_BOZUK = True
        log(">>> Kayitli kullanici adi/sifre ile GIRILEMEDI. Hesabin kilitlenmesin diye")
        log("    bir daha otomatik denenmeyecek. Sifre yanlissa programi kapat,")
        log("    'giris.dat' dosyasini sil ve yeniden baslat.")
    log(">>> Acilan tarayicida okula ELLE giris yap; program sonra kendisi devam eder.")
    bip(1)
    while time.time() < BITIS:
        bekle(3)
        if not giris_sayfasi_mi(s):
            log("Giris algilandi.")
            return True
    return False


# ======================= Ders ekranindaki pencereler =======================
def pencereleri_yonet(s):
    """Kayit uyarisi, ses secimi, aktiflik kontrolu pencerelerini halleder.
    Dinleyici olarak baglandiysa True."""
    m = sayfa_metni(s)
    if any(k in m for k in KAYIT_UYARISI):
        t = tikla(s, ILERLE)
        if t:
            log("Kayit uyarisi: '" + t + "' butonuna basildi.")
            bekle(1.5)
            m = sayfa_metni(s)
    if any(k in m for k in AKTIVITE):
        t = tikla(s, AKTIVITE_BUTON)
        if t:
            log("'Hala derste misin' kontrolu onaylandi ('" + t + "').")
    if any(k in m for k in SES_MODAL):
        t = tikla(s, SES_DINLE, yasak=MIKROFON_YASAK)
        if t:
            log("Ses: '" + t + "' secildi -> DINLEYICI, mikrofon KAPALI.")
            bekle(2)
            ekran_goruntusu(s)
            return True
    return False


# ======================= Derse katilma =======================
def katilmayi_dene(context):
    """Derse girmeyi dener. Ders ekranina girildiyse True."""
    if ders_sekmesi(context):
        return True
    s = calisma_sekmesi(context)
    fazla_sekmeleri_kapat(context, s)

    if not git(s, LINK):
        return False
    if giris_sayfasi_mi(s):
        if not giris_yap(s):
            return False
        if not git(s, LINK):
            return False

    ds = None
    son = time.time() + KATIL_BEKLEME
    while time.time() < son:
        ds = ders_sekmesi(context)
        if ds:
            break
        if not s.is_closed():
            t = tikla(s, OTURUMA_KATIL)
            if t:
                log("'" + t + "' butonuna basildi.")
                bekle(4)
                continue
        bekle(3)
    if not ds:
        log("Ders su an acik degil.")
        return False

    log("Ders ekranina girildi.")
    son = time.time() + SES_BEKLEME
    while time.time() < son:
        if ds.is_closed():
            break
        if pencereleri_yonet(ds):
            return True
        bekle(2)
    log("Ses secim penceresi cikmadi (derstesin, ses baglantisi olmayabilir).")
    ekran_goruntusu(ds)
    return True


# ======================= Ana dongu =======================
def tarayici_ac(p):
    DURUM["kapandi"] = False
    ctx = p.chromium.launch_persistent_context(
        str(PROFIL),
        headless=HEADLESS,
        viewport={"width": 1280, "height": 800},
        args=[
            "--disable-blink-features=AutomationControlled",
            # Gercek mikrofon/kamera yerine sahte cihaz: mikrofon asla acilamaz
            "--use-fake-device-for-media-stream",
            # Ders sesi tiklama olmadan calsin
            "--autoplay-policy=no-user-gesture-required",
            # Pencere arkada / oyun ustteyken tarayici uyumasin
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ],
    )
    ctx.on("close", lambda *_: DURUM.__setitem__("kapandi", True))
    try:
        ctx.clear_permissions()   # mikrofon/kamera izni yok
    except Exception:
        pass
    if KANCA:
        KANCA(ctx)
    return ctx


def izle(ctx):
    ders_acik = False
    hic_girildi = False
    son_deneme = float("-inf")
    hata = 0
    while time.time() < BITIS:
        _kapandiysa_firlat()
        try:
            ds = ders_sekmesi(ctx)
            if ds:
                if not ders_acik:
                    ders_acik = hic_girildi = True
                    log("Derstesin. Izleniyor...")
                pencereleri_yonet(ds)
            else:
                if ders_acik:
                    ders_acik = False
                    son_deneme = time.time()
                    log("Ders kapandi. " + str(TEKRAR_ARALIK // 60) + " dk'da bir tekrar denenecek.")
                aralik = TEKRAR_ARALIK if hic_girildi else ILK_DENEME_ARALIK
                if time.time() - son_deneme >= aralik:
                    son_deneme = time.time()
                    if katilmayi_dene(ctx):
                        ders_acik = hic_girildi = True
                        bip(2)
                        log("Derstesin. Izleniyor...")
                    elif time.time() < BITIS:
                        if aralik >= 60:
                            log(str(aralik // 60) + " dk sonra tekrar denenecek.")
                        else:
                            log(str(aralik) + " sn sonra tekrar denenecek.")
            hata = 0
        except TarayiciKapandi:
            raise
        except Exception as e:
            _kapandiysa_firlat()
            hata += 1
            log("Hata (program devam ediyor):", kisa(e))
            if hata >= 5:
                raise TarayiciKapandi()   # tarayiciyi bastan ac
        bekle(KONTROL_ARALIK)


def main():
    global KULLANICI, SIFRE, LINK, BITIS, OTO_GIRIS_BOZUK
    kilit = tek_kopya_kilidi()
    if kilit is None:
        log("Program ZATEN ACIK. Ikinci kez acmana gerek yok.")
        return
    try:
        kayit_kirp()
        try:
            PROFIL.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log("Klasore yazilamiyor:", kisa(e))
            log("Dosyalari zip'in icinden CIKARIP normal bir klasore koy (orn. Masaustu).")
            return

        KULLANICI, SIFRE, LINK = giris_bilgisi_oku()
        if not (KULLANICI and SIFRE and LINK):
            try:
                KULLANICI, SIFRE, LINK = giris_bilgisi_sor_ve_kaydet()
            except (EOFError, KeyboardInterrupt):
                log("Bilgi girilmedi, program kapaniyor.")
                return
        OTO_GIRIS_BOZUK = False

        BITIS = time.time() + TOPLAM_SURE
        log("Ders linki:", LINK)
        log("Program saat " + time.strftime("%H:%M", time.localtime(BITIS)) +
            "'de kendini kapatacak. Durdurmak icin bu siyah pencereyi kapat.")
        uyku_engelle()

        with sync_playwright() as p:
            acilamadi = 0
            while time.time() < BITIS:
                ctx = None
                try:
                    ctx = tarayici_ac(p)
                    acilamadi = 0
                    izle(ctx)
                    break
                except TarayiciKapandi:
                    if time.time() >= BITIS:
                        break
                    log("Tarayici kapandi. " + str(YENIDEN_AC_BEKLE) + " sn sonra yeniden aciliyor.")
                    time.sleep(YENIDEN_AC_BEKLE)
                except PWError as e:
                    if ctx is None:
                        acilamadi += 1
                        log("Tarayici acilamadi:", kisa(e))
                        if acilamadi >= 3:
                            log(">>> Tarayici acilamiyor. Programi 'baslat.bat' ile calistir.")
                            break
                    else:
                        log("Tarayici hatasi:", kisa(e))
                    time.sleep(10)
                except Exception as e:
                    log("Beklenmeyen hata, tarayici yeniden aciliyor:", kisa(e))
                    time.sleep(10)
                finally:
                    if ctx is not None:
                        try:
                            ctx.close()
                        except Exception:
                            pass
        if time.time() >= BITIS:
            log("Sure doldu, program kapandi.")
            if BILGISAYARI_KAPAT:
                bilgisayari_kapat()
        else:
            log("Program kapandi (sure dolmadan; bilgisayar kapatilmayacak).")
    finally:
        uyku_serbest()
        try:
            kilit.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Elle durduruldu.")
    except Exception as e:
        log("Beklenmeyen hata:", repr(e))
