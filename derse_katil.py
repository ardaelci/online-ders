# -*- coding: utf-8 -*-
"""
Biruni BEM (BirDeHa / Moodle + BigBlueButton) canli derse otomatik katilir.
Mikrofon ASLA acilmaz: ses secimi cikarsa "Yalniz dinle" secilir.

Akis (TAMAMEN OTOMATIK - ENTER beklemez):
 1) Tarayici acilir. Kayitli giris bilgisiyle otomatik giris yapar.
    (Ilk sefer terminalde kullanici adi + sifre sorar, bir daha sormaz.)
 2) Derse girmeyi dener. Ders henuz acik degilse 30 sn'de bir tekrar
    dener. Girince "Yalnizca dinleme" (mikrofon KAPALI) secer.
 3) 6 saat boyunca izler. Ders kapanirsa (dersler ~45 dk) 5 dk'da bir
    girene kadar tekrar dener. 6 saat dolunca program kendini kapatir.

Oturum "profil" klasorunde saklanir; ikinci calistirmada giris gerekmez.
"""
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==================== AYARLAR ====================
# Ders linki artik ILK CALISTIRMADA terminalden sorulur, elle degistirmeye
# gerek yok. (Degistirmek icin 'giris.dat' dosyasini sil, tekrar sorar.)
TEKRAR_ARALIK = 5 * 60        # ders kapaninca kac sn'de bir tekrar denesin (5 dk)
TOPLAM_SURE   = 6 * 60 * 60   # program toplam kac sn calissin (6 saat)
# =================================================

OKUL = "biruni.edu.tr"
KLASOR = Path(__file__).resolve().parent
PROFIL = KLASOR / "profil"
GIRIS_DOSYA = KLASOR / "giris.dat"   # kullanici adi + sifre + link (karistirilmis)

# Bu degerler main() basinda doldurulur
LINK = ""
KULLANICI = None
SIFRE = None

# Giris sayfasini tanimak icin (goruldugu kesin olanlar)
GIRIS_ISARETLERI = ["giriş yap", "giris yap", "kullanıcı adı", "kullanici adi",
                    "şifre", "sifre", "log in", "birdeha"]
# BBB oturumuna girme butonu (Moodle ara sayfasinda cikabilir)
OTURUMA_KATIL = ["Oturuma katıl", "Oturuma katil", "Join session", "Katıl", "Katil"]
# "Kayit aliniyor" uyarisinda cikan onay butonu -> once buna basilir.
ILERLE = ["İlerle", "Ilerle", "Devam et", "Continue", "Proceed"]
# "Ilerle"ye yalnizca su yazilar ekrandayken basilir (yanlis yere basmamak icin)
KAYIT_UYARISI = ["kayıt alınıyor", "kayit aliniyor", "kaydı kullanılabilecek",
                 "kaydi kullanilabilecek", "recording", "being recorded"]
# Ses modu secimi -> SADECE DINLEYICI. Mikrofon butonuna ASLA basma.
SES_DINLE = ["Yalnızca dinleme", "Yalnizca dinleme",
             "Yalnızca dinle", "Yalnizca dinle", "Yalnız dinle", "Yalniz dinle",
             "Dinleyici olarak katıl", "Dinleyici olarak katil",
             "Dinleyici", "Listen only", "Listen Only", "Only listen", "Dinle"]
# Bu yazilar gorunurse mikrofonla katiliyoruzdur -> sakin TIKLAMA.
MIKROFON_YASAK = ["mikrofon", "microphone", "mic'", "konuş", "konus", "speak"]
# Dersin bittigini gosteren yazilar
BITTI = ["oturum sona erdi", "oturum kapatıldı", "oturum kapatildi",
         "toplantı sona erdi", "toplanti sona erdi", "ders sona erdi",
         "session has ended", "meeting has ended", "meeting ended",
         "you have logged out", "görüşmeden ayrıldınız", "gorusmeden ayrildiniz"]


def log(*a):
    print("[" + time.strftime("%H:%M:%S") + "]", *a, flush=True)


# ---- Giris bilgisi: kaydet / oku ----
# Sifre duz metin DEGIL; base64 ile karistirilarak saklanir. Bu gercek bir
# sifreleme degildir, sadece dosyayi acan birinin ciplak gozle okumasini
# engeller. Bilgisayarini baskasiyla paylasiyorsan dikkatli ol.
def _karistir(metin):
    return base64.b64encode(metin.encode("utf-8")).decode("ascii")


def _coz(veri):
    try:
        return base64.b64decode(veri.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def giris_bilgisi_oku():
    """Kayitli (kullanici, sifre, link) dondurur; yoksa (None, None, None)."""
    if not GIRIS_DOSYA.exists():
        return None, None, None
    try:
        d = json.loads(GIRIS_DOSYA.read_text(encoding="utf-8"))
        return _coz(d.get("k", "")), _coz(d.get("s", "")), _coz(d.get("l", ""))
    except Exception:
        return None, None, None


def _link_gecerli_mi(link):
    """Biruni BBB ders linki gibi gorunuyor mu?"""
    link = link.lower()
    return link.startswith("http") and "biruni.edu.tr" in link and "bbb_view" in link


def giris_bilgisi_sor_ve_kaydet():
    """Terminalden kullanici adi + sifre + ders linki ister, karistirip
    kaydeder. Hepsi EKRANDA GORUNUR (bazi terminaller gizli giris almiyor)."""
    print("\n=== ILK KURULUM (bu bilgiler SADECE ILK SEFER sorulur) ===")
    print("Hepsi bu klasorde 'giris.dat' dosyasina kaydedilir, bir daha sorulmaz.")
    print("Degistirmek icin 'giris.dat' dosyasini sil, program tekrar sorar.")
    print("NOT: Yazdiklarin ekranda GORUNUR. Yaninda kimse yoksa sorun degil.\n")

    kullanici = ""
    while not kullanici:
        kullanici = input("Okul kullanici adin: ").strip()

    sifre = ""
    while not sifre:
        sifre = input("Okul sifren: ").strip()

    print("\nDers linki: Okul sisteminde canli derse sag tiklayip")
    print("'Baglanti adresini kopyala' de, sonra buraya yapistir.")
    print("(Linkte 'bbb_view' ve 'id=...' gecmeli.)")
    link = ""
    while True:
        link = input("Ders linki: ").strip()
        if not link:
            continue
        if _link_gecerli_mi(link):
            break
        print(">>> Bu link beklenen bicimde degil. Yine de kullanmak icin")
        onay = input("    'e' yazip Enter'a bas, ya da dogru linki yapistir: ").strip()
        if onay.lower() in ("e", "evet", "y", "yes"):
            break

    try:
        GIRIS_DOSYA.write_text(
            json.dumps({"k": _karistir(kullanici),
                        "s": _karistir(sifre),
                        "l": _karistir(link)}),
            encoding="utf-8",
        )
        log("Bilgiler kaydedildi (giris.dat).")
    except Exception as e:
        log("Bilgiler kaydedilemedi:", repr(e))
    return kullanici, sifre, link


def formu_doldur_giris_yap(s, kullanici, sifre):
    """Giris sayfasindaki kullanici adi/sifre alanlarini doldurup
    'Giris yap' butonuna basar. Basarili denemede True."""
    if not kullanici or not sifre:
        return False
    try:
        # Moodle giris formu: id'ler genelde 'username' ve 'password'
        kadi = None
        for sec in ("#username", "input[name='username']",
                    "input[autocomplete='username']"):
            try:
                if s.locator(sec).count() > 0:
                    kadi = s.locator(sec).first
                    break
            except Exception:
                continue
        sif = None
        for sec in ("#password", "input[type='password']",
                    "input[name='password']"):
            try:
                if s.locator(sec).count() > 0:
                    sif = s.locator(sec).first
                    break
            except Exception:
                continue
        if not kadi or not sif:
            return False
        kadi.fill(kullanici, timeout=4000)
        sif.fill(sifre, timeout=4000)
        # 'Giris yap' / 'Log in' butonu, yoksa Enter
        if not tikla(s, ["Giriş yap", "Giris yap", "Log in", "Oturum aç", "Oturum ac"]):
            sif.press("Enter")
        log("Giris formu dolduruldu, giris yapiliyor...")
        time.sleep(4)
        return True
    except Exception as e:
        log("Form doldurulamadi:", repr(e))
        return False


def bip(kez=3):
    try:
        import winsound
        for _ in range(kez):
            winsound.Beep(1100, 350)
            time.sleep(0.1)
    except Exception:
        # Windows disi / ses yoksa sessiz gec
        print("\a", end="", flush=True)


# ---- Uyku modu yonetimi (sadece Windows) ----
# Program calisirken uyku kapatilir, program BITINCE (nasil biterse bitsin)
# eski ayar geri yuklenir.
def _powercfg_uyku_oku():
    """Prize bagliyken (AC) mevcut uyku suresini saniye cinsinden okur.
    Okunamazsa None doner."""
    try:
        cikti = subprocess.run(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return None
    # "Current AC Power Setting Index: 0x0000070e" / TR: "Gecerli AC ... : 0x..."
    bulunan = re.findall(r"AC[^\n]*?:\s*0x([0-9a-fA-F]+)", cikti)
    if not bulunan:
        return None
    try:
        return int(bulunan[0], 16)  # saniye
    except Exception:
        return None


def uyku_kapat():
    """Uykuyu kapatir. Geri yuklemek icin eski degeri (saniye) dondurur."""
    if sys.platform != "win32":
        return None
    eski = _powercfg_uyku_oku()
    try:
        subprocess.run(["powercfg", "/change", "standby-timeout-ac", "0"], timeout=15)
        subprocess.run(["powercfg", "/change", "hibernate-timeout-ac", "0"], timeout=15)
        log("Uyku modu gecici olarak kapatildi.")
    except Exception as e:
        log("Uyku kapatilamadi:", repr(e))
    return eski


def uyku_geri_yukle(eski_saniye):
    """Uyku ayarini eski haline dondurur. Eski deger okunamadiysa
    guvenli varsayilan olan 30 dk'ya ayarlar."""
    if sys.platform != "win32":
        return
    dakika = 30 if not eski_saniye else max(0, eski_saniye // 60)
    try:
        subprocess.run(["powercfg", "/change", "standby-timeout-ac", str(dakika)],
                       timeout=15)
        if eski_saniye:
            log("Uyku ayari eski haline donduruldu (" + str(dakika) + " dk).")
        else:
            log("Eski uyku ayari okunamadi; guvenli deger 30 dk'ya ayarlandi.")
    except Exception as e:
        log("Uyku ayari geri yuklenemedi:", repr(e))


def sayfa_metni(s):
    try:
        return (s.inner_text("body", timeout=3000) or "").lower()
    except Exception:
        return ""


def giris_sayfasi_mi(s):
    try:
        u = s.url.lower()
    except Exception:
        return False
    if "login" in u or "wantsurl" in u:
        return True
    m = sayfa_metni(s)
    if not m:
        return False
    # Giris formu isaretlerinden en az ikisi varsa giris sayfasidir
    return sum(1 for k in GIRIS_ISARETLERI if k in m) >= 2


def ders_bitti_mi(s):
    m = sayfa_metni(s)
    return any(k in m for k in BITTI)


def tikla(s, yazilar, timeout=1200):
    """Verilen yazilardan birini buton/link/metin olarak bulup tiklar."""
    for yazi in yazilar:
        desen = re.compile(re.escape(yazi), re.I)
        for bulucu in (
            lambda d=desen: s.get_by_role("button", name=d),
            lambda d=desen: s.get_by_role("link", name=d),
            lambda d=desen: s.get_by_text(d),
        ):
            try:
                hedef = bulucu().first
                if hedef.count() == 0:
                    continue
                hedef.click(timeout=timeout)
                return yazi
            except Exception:
                continue
    return None


def dinleyici_sec(s, timeout=1500):
    """SADECE dinleyici butonunu secer. Icinde mikrofon/konus gecen bir
    secenege ASLA basmaz. Dinleyici butonu bulunursa yazisini, yoksa None."""
    for yazi in SES_DINLE:
        desen = re.compile(re.escape(yazi), re.I)
        for bulucu in (
            lambda d=desen: s.get_by_role("button", name=d),
            lambda d=desen: s.get_by_role("link", name=d),
            lambda d=desen: s.get_by_text(d),
        ):
            try:
                hedef = bulucu().first
                if hedef.count() == 0:
                    continue
                # Buton metninde mikrofon/konus geciyorsa dokunma
                try:
                    etiket = (hedef.inner_text(timeout=600) or "").lower()
                except Exception:
                    etiket = ""
                if any(k in etiket for k in MIKROFON_YASAK):
                    continue
                hedef.click(timeout=timeout)
                return yazi
            except Exception:
                continue
    return None


def ders_sekmesi(context):
    """Acik, canli gorunen bir ders sekmesi varsa dondurur; yoksa None.
    Bitmis sekmeleri kapatir. Giris sayfalarini saymaz."""
    for s in list(context.pages):
        try:
            u = s.url
        except Exception:
            continue
        if not u.startswith("http"):
            continue
        if giris_sayfasi_mi(s):
            continue
        if ders_bitti_mi(s):
            log("Bir sekmede ders bitmis gorunuyor, kapatiliyor.")
            try:
                s.close()
            except Exception:
                pass
            continue
        # Okul alanindaki bir ara sayfa VEYA BBB sunucu sekmesi: ders olabilir
        return s
    return None


def calisma_sekmesi(context):
    """Gezinmek icin bir sekme dondurur (varsa okul sekmesi)."""
    for s in context.pages:
        try:
            if OKUL in s.url or s.url in ("about:blank", ""):
                return s
        except Exception:
            continue
    return context.pages[0] if context.pages else context.new_page()


def giris_bekle(context, kullanici=None, sifre=None):
    """Giris yapilana kadar bekler. Kayitli bilgi varsa formu otomatik
    doldurup giris yapar. Zaten giris varsa hemen doner."""
    s = calisma_sekmesi(context)
    try:
        s.goto(LINK, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        pass

    uyarildi = False
    oto_denendi = 0
    while giris_sayfasi_mi(s):
        # Once kayitli bilgiyle otomatik giris dene (en fazla 3 kez)
        if kullanici and sifre and oto_denendi < 3:
            oto_denendi += 1
            if formu_doldur_giris_yap(s, kullanici, sifre):
                time.sleep(3)
                if not giris_sayfasi_mi(s):
                    break  # giris basarili
                else:
                    log("Otomatik giris tutmadi (bilgi yanlis olabilir).")
            # dogru link'e geri don
            try:
                if OKUL in s.url and "bbb_view" not in s.url:
                    s.goto(LINK, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            continue

        # Otomatik giris yoksa veya basaramadiysa: elle giris bekle
        if not uyarildi:
            if kullanici:
                log(">>> Otomatik giris yapilamadi. Acilan tarayicida ELLE giris yap.")
                log("    (Bilgiyi degistirmek icin 'giris.dat' dosyasini sil.)")
            else:
                log(">>> Acilan tarayicida OKULA GIRIS YAP. Giris yapinca program devam eder.")
            bip(1)
            uyarildi = True
        time.sleep(3)
        try:
            if OKUL in s.url and "bbb_view" not in s.url:
                s.goto(LINK, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

    if uyarildi or oto_denendi:
        log("Giris algilandi.")
    return s


def katilmayi_dene(context):
    """Derse girmeyi dener. Girildi/girilmis ise True."""
    if ders_sekmesi(context):
        return True

    s = giris_bekle(context, KULLANICI, SIFRE)
    # Link'i (yeniden) ac
    try:
        if "bbb_view" not in s.url:
            s.goto(LINK, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    # Moodle ara sayfasinda "Oturuma katil" butonu olabilir; ~45 sn dene
    girildi = False
    for _ in range(15):
        if ders_sekmesi(context):
            girildi = True
            break
        tiklanan = tikla(s, OTURUMA_KATIL)
        if tiklanan:
            log("'" + tiklanan + "' butonuna basildi.")
            time.sleep(4)
            if ders_sekmesi(context):
                girildi = True
                break
        else:
            time.sleep(3)

    if not girildi:
        # Ara sayfada "katil" hic cikmadiysa ders muhtemelen acilmamis
        if giris_sayfasi_mi(s):
            return False
        log("Ders su an acik gorunmuyor.")
        return False

    # Once "Kayit aliniyor" uyarisi cikarsa "Ilerle"ye bas, sonra ses
    # penceresinde SADECE "Yalnizca dinleme / Dinleyici" sec ~90 sn.
    # Mikrofon butonuna ASLA basilmaz.
    for _ in range(45):
        for sk in list(context.pages):
            # 1) SADECE "kayit aliniyor" uyarisi ekrandaysa "Ilerle"ye bas
            if any(k in sayfa_metni(sk) for k in KAYIT_UYARISI):
                ilerledi = tikla(sk, ILERLE)
                if ilerledi:
                    log("'" + ilerledi + "' butonuna basildi (kayit uyarisi).")
                    time.sleep(1.5)
            # 2) Ses modu -> Yalnizca dinleme
            tiklanan = dinleyici_sec(sk)
            if tiklanan:
                log("Derse DINLEYICI olarak girildi -> '" + tiklanan + "' (mikrofon KAPALI).")
                try:
                    sk.screenshot(path=str(KLASOR / "son_katilim.png"))
                except Exception:
                    pass
                return True
        time.sleep(2)

    log("Ders sekmesi acildi; ses secimi gorunmedi (zaten baglanmis olabilir).")
    try:
        ders_sekmesi(context).screenshot(path=str(KLASOR / "son_katilim.png"))
    except Exception:
        pass
    return True


def main():
    global KULLANICI, SIFRE, LINK
    PROFIL.mkdir(exist_ok=True)

    # Giris bilgisi + link: kayitliysa oku, degilse bir kez sor ve kaydet
    KULLANICI, SIFRE, LINK = giris_bilgisi_oku()
    if not KULLANICI or not SIFRE or not LINK:
        KULLANICI, SIFRE, LINK = giris_bilgisi_sor_ve_kaydet()
    log("Ders linki: " + LINK)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFIL),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                # Gercek mikrofon/kamera yerine SAHTE (bos) cihaz kullan:
                # yazilim hata yapsa bile gercek mikrofonun ACILAMAZ.
                "--use-fake-device-for-media-stream",
            ],
        )
        # Hicbir siteye mikrofon/kamera izni VERME. Boylece kazara
        # mikrofon acma ihtimali tamamen ortadan kalkar; biz zaten
        # yalnizca dinleyici olarak katiliyoruz.
        try:
            context.clear_permissions()
        except Exception:
            pass

        log("Baslatiliyor...")
        # 1) Derse ilk giris (ders acilana kadar dener)
        while not katilmayi_dene(context):
            log("Girilemedi, 30 sn sonra tekrar denenecek. (Durdurmak: pencereyi kapat / CTRL+C)")
            time.sleep(30)

        bip(3)
        log("Derse girildi. Izleme OTOMATIK baslatiliyor (ENTER beklenmiyor).")

        # 2) Izleme donemi
        bitis = time.time() + TOPLAM_SURE
        log("Izleme basladi. Bitis saati:", time.strftime("%H:%M", time.localtime(bitis)))
        ders_acik = True
        son_deneme = 0.0
        while time.time() < bitis:
            try:
                var = ders_sekmesi(context) is not None
            except Exception:
                var = True  # emin degilsek dokunma

            if var:
                if not ders_acik:
                    log("Ders tekrar acildi.")
                    ders_acik = True
            else:
                if ders_acik:
                    ders_acik = False
                    son_deneme = time.time()
                    log("Ders kapandi. " + str(TEKRAR_ARALIK // 60) + " dk sonra tekrar denenecek.")
                elif time.time() - son_deneme >= TEKRAR_ARALIK:
                    log("Linke tekrar giriliyor...")
                    try:
                        if katilmayi_dene(context):
                            ders_acik = True
                            bip(2)
                            log("Yeniden girildi.")
                        else:
                            log("Henuz acilmamis.")
                    except Exception as e:
                        log("Deneme sirasinda hata:", repr(e))
                    son_deneme = time.time()

            time.sleep(10)

        log("6 saat doldu. Program kapaniyor.")
        try:
            context.close()
        except Exception:
            pass


if __name__ == "__main__":
    _eski_uyku = uyku_kapat()      # uykuyu kapat, eski degeri sakla
    try:
        main()
    except KeyboardInterrupt:
        log("Elle durduruldu.")
    except Exception as e:
        log("Beklenmeyen hata:", repr(e))
    finally:
        # Program NASIL biterse bitsin uyku ayari geri yuklenir.
        uyku_geri_yukle(_eski_uyku)
        try:
            input("\nKapatmak icin ENTER'a bas...")
        except Exception:
            pass
