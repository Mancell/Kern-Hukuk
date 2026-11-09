"""
Resmî Gazete (T.C. Resmî Gazete) günlük fihrist + madde metni — resmigazete.gov.tr.

`aihm.py`/`kik.py` kardeşi. Kaynak: kullanıcının verdiği
github.com/emircanok/resmi-gazete-mcp (MIT) — kod BİREBİR kopyalanmadı
(TypeScript, cheerio/undici'ye bağımlı), yalnız YAKLAŞIMI ölçülüp bu modülde
projenin kendi kalıbıyla (regex tabanlı ayrıştırma, `httpx`, Türkçe adlandırma)
yeniden yazıldı — "başkasına bağımlı olmayalım" (kullanıcı kararı).

SÖZLEŞME CANLI ÖLÇÜLDÜ (2026-09-14), referans depodan yola çıkılıp doğrulandı:
  - `GET /eskiler/{yyyy}/{aa}/{yyyyaagg}{Mn}.htm` → o günün fihristi (bölümler +
    madde bağlantıları). `Mn` (M1, M2, …) mükerrer sayı — o gün birden fazla
    Resmî Gazete çıkarsa. Gün yoksa (Pazar/tatil/mükerrer yok) HTTP 404.
  - `GET /eskiler/{yyyy}/{aa}/{yyyyaagg}{Mn}-{sira}.htm` → tek madde (yönetmelik/
    karar/ilan metni).
  - **Kodlama Windows-1254 (Türkçe), UTF-8 DEĞİL** — HTTP `Content-Type`
    başlığı bunu SÖYLEMİYOR (yalnız "text/html"), yalnız HTML içindeki
    `<meta charset=...>` etiketinde var. Meta etiketi de yoksa `cp1254`'e
    düşülüyor (ÖLÇÜLDÜ: bu sitenin HER sayfası bu kodlama — referans depo
    "utf-8"e düşüyordu, o yanlış olurdu). Kodlama yanlış çözülürse Türkçe
    karakterler (İ, ş, ğ, ı, ö, ü, Ç) bozuk çıkar — sessiz veri kalitesi
    hatası, o yüzden HTTP başlığına güvenilmiyor.
  - **UA'sız istek bağlantıyı sıfırlıyor** (curl UA'sız `HTTP 000` döndü,
    UA'lı `200`) — referans depo da aynı notu düşmüş, canlı doğrulandı.
  - Sayfalar Word'den dışa aktarılmış HTML (mso-* stilleri, iç içe `<div>`) —
    UYAP/KİK karar metinleriyle AYNI aile; `_metne_cevir` aynı yaklaşımla
    (blok etiketleri satır sonuna çevir, kalanını at) düz metne indiriyor.
  - Bölüm başlıkları (`YÜRÜTME VE İDARE BÖLÜMÜ`, `İLÂN BÖLÜMÜ`) `<b><u>...`
    içinde ama iç etiket TUTARSIZ (bazen `<font>`, bazen `<span style=...>`)
    — ayrıştırıcı iç etikete değil `<b><u>...</u></b>` zarfına bakıyor.
  - Madde bağlantıları göreli (`20260910-1.htm`) — fihristin KENDİ
    dizinine göre çözülüyor.

v1 kapsamı bilerek dar: yalnız `fihrist_getir` (bir günün listesi) ve
`madde_getir` (tek maddenin düz metni). Referans depodaki `search_gazette`
(tarih aralığında çok günlük fihrist taraması, tam metin değil yalnız
BAŞLIK eşleşmesi) ve `extract_pdf_text` v1'e ALINMADI — ilki onlarca isteği
tek kullanıcı aksiyonuna bağlıyor (pahalı + EKAP/KİK'te olduğu gibi ölçülmemiş
bir yük), ikincisi ayrı bir iş. Kullanıcı isterse v2.

Hız sınırı: `bedesten.py`'nin "adalet kotası" deseni (`_Kova`/`CokHizli`)
burada da var — RG'de sert bir sunucu limiti ÖLÇÜLMEDİ (birkaç sıralı istekte
engel yok) ama kural bilinçli standart: her yeni dış kaynakta "her kullanıcı
kendi havuzundan" (kullanıcı kararı).

Gizlilik: yalnızca tarih/madde URL'i (avukatın seçtiği) Resmî Gazete'ye
gider — kamu kurumu, üçüncü taraf yok. Loglanmaz.

Tek başına çalışır:
    python server/resmi_gazete.py 2026-09-10
"""
import html
import os
import re
import ssl

import httpx

from bedesten import CokHizli
from bedesten import _Kova as _KovaSinifi

BASE = "https://www.resmigazete.gov.tr"

# resmigazete.gov.tr TLS el sıkışmasında yaprak sertifikayı gönderiyor ama ARA
# sertifikayı (GeoTrust TLS RSA CA G1) GÖNDERMİYOR — canlı ölçüldü:
# `openssl s_client` de aynı `unable to get local issuer certificate`
# hatasını veriyor, yani eksik olan sunucu tarafı, istemcimiz değil.
# Tarayıcılar/curl bunu AIA (Authority Information Access) ile otomatik
# tamamlıyor, Python'un stdlib `ssl`'i tamamlamıyor. Çözüm doğrulamayı
# KAPATMAK değil eksik halkayı TAMAMLAMAK: bu ara sertifika yaprak
# sertifikanın AIA alanındaki resmi adresten (cacerts.geotrust.com) alındı —
# gizli olmayan, yıllarca geçerli genel bir CA sertifikası; zincir yine UÇTAN
# UCA kriptografik olarak doğrulanıyor (`openssl` ile `verify return code: 0`
# doğrulandı), yalnız sunucunun atladığı halka ekleniyor.
_GEOTRUST_ARA_SERTIFIKA = """-----BEGIN CERTIFICATE-----
MIIEjTCCA3WgAwIBAgIQDQd4KhM/xvmlcpbhMf/ReTANBgkqhkiG9w0BAQsFADBh
MQswCQYDVQQGEwJVUzEVMBMGA1UEChMMRGlnaUNlcnQgSW5jMRkwFwYDVQQLExB3
d3cuZGlnaWNlcnQuY29tMSAwHgYDVQQDExdEaWdpQ2VydCBHbG9iYWwgUm9vdCBH
MjAeFw0xNzExMDIxMjIzMzdaFw0yNzExMDIxMjIzMzdaMGAxCzAJBgNVBAYTAlVT
MRUwEwYDVQQKEwxEaWdpQ2VydCBJbmMxGTAXBgNVBAsTEHd3dy5kaWdpY2VydC5j
b20xHzAdBgNVBAMTFkdlb1RydXN0IFRMUyBSU0EgQ0EgRzEwggEiMA0GCSqGSIb3
DQEBAQUAA4IBDwAwggEKAoIBAQC+F+jsvikKy/65LWEx/TMkCDIuWegh1Ngwvm4Q
yISgP7oU5d79eoySG3vOhC3w/3jEMuipoH1fBtp7m0tTpsYbAhch4XA7rfuD6whU
gajeErLVxoiWMPkC/DnUvbgi74BJmdBiuGHQSd7LwsuXpTEGG9fYXcbTVN5SATYq
DfbexbYxTMwVJWoVb6lrBEgM3gBBqiiAiy800xu1Nq07JdCIQkBsNpFtZbIZhsDS
fzlGWP4wEmBQ3O67c+ZXkFr2DcrXBEtHam80Gp2SNhou2U5U7UesDL/xgLK6/0d7
6TnEVMSUVJkZ8VeZr+IUIlvoLrtjLbqugb0T3OYXW+CQU0kBAgMBAAGjggFAMIIB
PDAdBgNVHQ4EFgQUlE/UXYvkpOKmgP792PkA76O+AlcwHwYDVR0jBBgwFoAUTiJU
IBiV5uNu5g/6+rkS7QYXjzkwDgYDVR0PAQH/BAQDAgGGMB0GA1UdJQQWMBQGCCsG
AQUFBwMBBggrBgEFBQcDAjASBgNVHRMBAf8ECDAGAQH/AgEAMDQGCCsGAQUFBwEB
BCgwJjAkBggrBgEFBQcwAYYYaHR0cDovL29jc3AuZGlnaWNlcnQuY29tMEIGA1Ud
HwQ7MDkwN6A1oDOGMWh0dHA6Ly9jcmwzLmRpZ2ljZXJ0LmNvbS9EaWdpQ2VydEds
b2JhbFJvb3RHMi5jcmwwPQYDVR0gBDYwNDAyBgRVHSAAMCowKAYIKwYBBQUHAgEW
HGh0dHBzOi8vd3d3LmRpZ2ljZXJ0LmNvbS9DUFMwDQYJKoZIhvcNAQELBQADggEB
AIIcBDqC6cWpyGUSXAjjAcYwsK4iiGF7KweG97i1RJz1kwZhRoo6orU1JtBYnjzB
c4+/sXmnHJk3mlPyL1xuIAt9sMeC7+vreRIF5wFBC0MCN5sbHwhNN1JzKbifNeP5
ozpZdQFmkCo+neBiKR6HqIA+LMTMCMMuv2khGGuPHmtDze4GmEGZtYLyF8EQpa5Y
jPuV6k2Cr/N3XxFpT3hRpt/3usU/Zb9wfKPtWpoznZ4/44c1p9rzFcZYrWkj3A+7
TNBJE0GmP2fhXhP1D/XVfIW/h0yCJGEiV9Glm/uGOa3DXHlmbAcxSyCRraG+ZBkA
7h4SeM6Y8l/7MBRpPCz6l8Y=
-----END CERTIFICATE-----"""

_TLS_BAGLAM = ssl.create_default_context()
_TLS_BAGLAM.load_verify_locations(cadata=_GEOTRUST_ARA_SERTIFIKA)
_TARIH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MADDE_YOL_RE = re.compile(r"/eskiler/\d{4}/\d{2}/\d{8}(M\d+)?-\d+\.htm$", re.I)
_META_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?([\w-]+)', re.I)
_CT_CHARSET_RE = re.compile(r'charset\s*=\s*"?\'?([^"\';\s]+)', re.I)

_KULLANICI_ARALIK_SN = float(os.getenv("RG_KULLANICI_ARALIK_SN", "1.5"))
_MAKS_BEKLEME_SN = float(os.getenv("RG_MAKS_BEKLEME_SN", "8"))
_kullanici_kovalari: dict[str, _KovaSinifi] = {}


def _kullanici_kovasi(kimlik: str) -> _KovaSinifi:
    kova = _kullanici_kovalari.get(kimlik)
    if kova is None:
        kova = _KovaSinifi(_KULLANICI_ARALIK_SN)
        _kullanici_kovalari[kimlik] = kova
    return kova


class ResmiGazeteHatasi(Exception):
    """Kullanıcıya olduğu gibi gösterilebilecek Türkçe hata."""


_istemci: httpx.AsyncClient | None = None


def _get() -> httpx.AsyncClient:
    global _istemci
    if _istemci is None:
        _istemci = httpx.AsyncClient(
            base_url=BASE,
            timeout=20.0,
            follow_redirects=True,
            verify=_TLS_BAGLAM,
            headers={"User-Agent": "Mozilla/5.0 (compatible; mancel-rg/1.0)"},
        )
    return _istemci


async def kapat() -> None:
    global _istemci
    if _istemci is not None:
        await _istemci.aclose()
        _istemci = None


def _kodlama_bul(ham: bytes, content_type_basligi: str) -> str:
    m = _CT_CHARSET_RE.search(content_type_basligi or "")
    if m:
        return m.group(1).lower()
    m = _META_CHARSET_RE.search(ham[:2048])
    if m:
        return m.group(1).decode("ascii", errors="ignore").lower()
    return "cp1254"  # ölçülen varsayılan — bkz. modül başlığı


def _metne_coz(yanit: httpx.Response) -> str:
    kodlama = _kodlama_bul(yanit.content, yanit.headers.get("content-type", ""))
    try:
        return yanit.content.decode(kodlama, errors="replace")
    except LookupError:
        return yanit.content.decode("cp1254", errors="replace")


def _duz_metin(parca: str) -> str:
    parca = re.sub(r"<[^>]+>", " ", parca)
    metin = html.unescape(parca)
    return re.sub(r"\s+", " ", metin).strip()


def _baslik_temizle(metin: str) -> str:
    return re.sub(r"^[\s–—•.\-–—•]+", "", metin).strip()


def _tarihi_dogrula(tarih: str) -> tuple[str, str, str]:
    if not _TARIH_RE.match(tarih or ""):
        raise ResmiGazeteHatasi("Geçersiz tarih (YYYY-AA-GG bekleniyor).")
    y, ay, gun = tarih.split("-")
    return y, ay, gun


def _fihrist_yolu(tarih: str, mukerrer: int = 0) -> str:
    y, ay, gun = _tarihi_dogrula(tarih)
    ek = f"M{mukerrer}" if mukerrer > 0 else ""
    return f"/eskiler/{y}/{ay}/{y}{ay}{gun}{ek}.htm"


_BASLIK_LINK_RE = re.compile(
    r"<b>\s*<u>(?P<baslik>.*?)</u>\s*</b>"
    r'|<a\s+href="(?P<href>[^"]+\.htm)"[^>]*>(?P<link_metin>.*?)</a>',
    re.I | re.S,
)
_SAYI_RE = re.compile(r"Tarihli\s+ve\s+(\d+)\s+Say[ıi]l[ıi]", re.I | re.S)
_PDF_RE = re.compile(r'href="(\d{8}(?:M\d+)?\.pdf)"', re.I)

# Resmî Gazete'nin sabit, anayasal/kanuni dört bölümü (Anayasa m.104, 2 Ocak
# 1961 tarihli 130 sayılı Kanun) — bir gün bunlardan biri hiç görünmüyorsa
# "o gün o bölümde madde yok" demektir, eksik veri değil. Ölçülen HTML'de
# bölüm başlığı yalnız içerik varsa render ediliyor (bkz. `_fihristi_ayikla`).
_KANONIK_BOLUMLER = [
    ("Yasama Bölümü", re.compile(r"YASAMA", re.I)),
    ("Yürütme ve İdare Bölümü", re.compile(r"Y[ÜU]R[ÜU]TME", re.I)),
    ("Yargı Bölümü", re.compile(r"YARGI", re.I)),
    ("İlan Bölümü", re.compile(r"İL[ÂA]N|ILAN", re.I)),
]


def _fihristi_ayikla(govde_html: str, tarih: str, kaynak_url: str) -> dict:
    y, ay, gun = tarih.split("-")
    dizin = f"{BASE}/eskiler/{y}/{ay}/"
    bolumler: list[dict] = []
    mevcut: dict | None = None

    for m in _BASLIK_LINK_RE.finditer(govde_html):
        if m.group("baslik") is not None:
            metin = _duz_metin(m.group("baslik"))
            if metin and re.search(r"B[ÖO]L[ÜU]M", metin, re.I):
                mevcut = {"ad": metin, "maddeler": []}
                bolumler.append(mevcut)
            continue

        href = m.group("href") or ""
        if not href or "fihrist" in href.lower() or "index" in href.lower():
            continue
        if not re.search(r"\d{8}(M\d+)?-\d+\.htm$", href, re.I):
            continue
        baslik = _baslik_temizle(_duz_metin(m.group("link_metin") or ""))
        if not baslik:
            continue
        if mevcut is None:
            mevcut = {"ad": "Genel", "maddeler": []}
            bolumler.append(mevcut)
        tam_url = href if href.startswith("http") else dizin + href
        mevcut["maddeler"].append({"baslik": baslik, "url": tam_url})

    bulunan = set()
    for b in bolumler:
        for kanonik_ad, desen in _KANONIK_BOLUMLER:
            if desen.search(b["ad"]):
                bulunan.add(kanonik_ad)
                break
    eksik_bolumler = [ad for ad, _ in _KANONIK_BOLUMLER if ad not in bulunan]

    sayi_m = _SAYI_RE.search(govde_html)
    pdf_m = _PDF_RE.search(govde_html)

    return {
        "tarih": tarih,
        "sayi": sayi_m.group(1) if sayi_m else None,
        "kaynak_url": kaynak_url,
        "pdf_url": f"{dizin}{pdf_m.group(1)}" if pdf_m else f"{dizin}{y}{ay}{gun}.pdf",
        "bolumler": bolumler,
        "eksik_bolumler": eksik_bolumler,
    }


def _madde_metnini_ayikla(sayfa_html: str) -> str:
    parca = re.sub(r"<head\b[^>]*>.*?</head>", " ", sayfa_html, flags=re.I | re.S)
    parca = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", parca, flags=re.I | re.S)
    parca = re.sub(r"<(br|p|div|tr|li|h[1-6])\b[^>]*/?>", "\n", parca, flags=re.I)
    parca = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", parca, flags=re.I)
    metin = re.sub(r"<[^>]+>", "", parca)
    metin = html.unescape(metin)
    metin = metin.replace("\r\n", "\n").replace("\r", "\n")
    metin = re.sub(r"[ \t]+", " ", metin)
    metin = re.sub(r"\n[ \t]+", "\n", metin)
    metin = re.sub(r"\n{3,}", "\n\n", metin)
    return metin.strip()


async def fihrist_getir(tarih: str, mukerrer: int = 0, kimlik: str = "anon") -> dict:
    """Bir günün fihristi (bölümler + madde başlık/bağlantıları).

    Gün yoksa (Pazar/tatil ya da o mükerrer sayısı yayımlanmadıysa) hata
    FIRLATMAZ — `bulundu: False` + boş `bolumler` döner, çağıran "bu günde
    gazete yok" diye gösterebilir (Optional dönüş yerine sabit şekil).
    """
    yol = _fihrist_yolu(tarih, mukerrer)
    if kimlik != "anon":
        await _kullanici_kovasi(kimlik).jeton(_MAKS_BEKLEME_SN)
    try:
        yanit = await _get().get(yol)
    except httpx.RequestError:
        raise ResmiGazeteHatasi("Resmî Gazete servisine ulaşılamadı.")
    if yanit.status_code == 404:
        return {
            "tarih": tarih,
            "sayi": None,
            "kaynak_url": f"{BASE}{yol}",
            "pdf_url": None,
            "bulundu": False,
            "bolumler": [],
            "eksik_bolumler": [ad for ad, _ in _KANONIK_BOLUMLER],
        }
    if yanit.status_code != 200:
        raise ResmiGazeteHatasi(f"Fihrist alınamadı (HTTP {yanit.status_code}).")
    govde = _metne_coz(yanit)
    sonuc = _fihristi_ayikla(govde, tarih, f"{BASE}{yol}")
    sonuc["bulundu"] = True
    return sonuc


async def madde_getir(url: str, kimlik: str = "anon") -> dict:
    """Tek bir madde/karar sayfasının düz metnini getirir.

    `url` yalnızca `resmigazete.gov.tr/eskiler/.../*.htm` biçiminde kabul
    edilir — sunucumuz keyfi bir adrese vekil (proxy) olmasın diye.
    """
    try:
        ayrisik = httpx.URL(url)
    except Exception:
        raise ResmiGazeteHatasi("Geçersiz adres.")
    if ayrisik.host != "www.resmigazete.gov.tr" or not _MADDE_YOL_RE.search(ayrisik.path):
        raise ResmiGazeteHatasi("Adres Resmî Gazete madde sayfası olmalı.")

    if kimlik != "anon":
        await _kullanici_kovasi(kimlik).jeton(_MAKS_BEKLEME_SN)
    try:
        yanit = await _get().get(str(ayrisik))
    except httpx.RequestError:
        raise ResmiGazeteHatasi("Resmî Gazete servisine ulaşılamadı.")
    if yanit.status_code != 200:
        raise ResmiGazeteHatasi(f"Madde alınamadı (HTTP {yanit.status_code}).")
    govde = _metne_coz(yanit)
    metin = _madde_metnini_ayikla(govde)
    if not metin:
        raise ResmiGazeteHatasi("Madde metni sayfada bulunamadı.")
    return {"url": str(ayrisik), "metin": metin}


# --------------------------------------------------------------------------
# Duman testi:  python server/resmi_gazete.py 2026-09-10
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    assert _fihrist_yolu("2026-09-10") == "/eskiler/2026/09/20260910.htm"
    assert _fihrist_yolu("2026-09-10", 2) == "/eskiler/2026/09/20260910M2.htm"
    try:
        _fihrist_yolu("10-09-2026")
        raise AssertionError("geçersiz tarih sessizce geçti")
    except ResmiGazeteHatasi:
        pass

    ornek = (
        '<a href="20260910.pdf"><img></a>'
        "<p>10 Eylül 2026 Tarihli ve 33366 Sayılı Resmî Gazete</p>"
        '<p><span class="Normal1"><b><u><font color="#000080">YÜRÜTME '
        "VE İDARE BÖLÜMÜ</font></u></b></span></p>"
        '<p><span class="Normal1"><font size="1">'
        '<a href="20260910-1.htm" style="text-decoration: none">––&nbsp; '
        "Test Yönetmeliği</a></font></span></p>"
        '<p><b><u><span style="color:navy">İLÂN BÖLÜMÜ</span></u></b></p>'
        '<p><a href="20260910-2.htm">• Bir ilan başlığı</a></p>'
    )
    f = _fihristi_ayikla(ornek, "2026-09-10", "https://x/eskiler/2026/09/20260910.htm")
    assert len(f["bolumler"]) == 2
    assert f["bolumler"][0]["ad"] == "YÜRÜTME VE İDARE BÖLÜMÜ"
    assert f["bolumler"][0]["maddeler"] == [
        {"baslik": "Test Yönetmeliği", "url": "https://www.resmigazete.gov.tr/eskiler/2026/09/20260910-1.htm"}
    ]
    assert f["bolumler"][1]["ad"] == "İLÂN BÖLÜMÜ"
    assert f["bolumler"][1]["maddeler"][0]["baslik"] == "Bir ilan başlığı"
    assert f["sayi"] == "33366"
    assert f["pdf_url"] == "https://www.resmigazete.gov.tr/eskiler/2026/09/20260910.pdf"
    assert f["eksik_bolumler"] == ["Yasama Bölümü", "Yargı Bölümü"]

    ic = "<head><style>.x{}</style></head><body><p>Birinci.</p><p>İkinci &amp; satır.</p></body>"
    assert _madde_metnini_ayikla(ic) == "Birinci.\n\nİkinci & satır."

    assert _kodlama_bul(b'<meta charset="Windows-1254">', "text/html") == "windows-1254"
    assert _kodlama_bul(b"<html></html>", "text/html; charset=UTF-8") == "utf-8"
    assert _kodlama_bul(b"<html></html>", "text/html") == "cp1254"
    print("birim: tamam")

    async def _dene() -> None:
        tarih = sys.argv[1] if len(sys.argv) > 1 else "2026-09-10"
        f = await fihrist_getir(tarih)
        if not f["bulundu"]:
            print(f"{tarih}: gazete yok (Pazar/tatil olabilir)")
            return
        toplam = sum(len(b["maddeler"]) for b in f["bolumler"])
        print(f"{tarih}: Sayı {f['sayi']} — {len(f['bolumler'])} bölüm, {toplam} madde")
        print(f"eksik bölümler: {f['eksik_bolumler']} | pdf: {f['pdf_url']}")
        assert toplam, "hiç madde bulunamadı"
        ilk_bolum = f["bolumler"][0]
        ilk_madde = ilk_bolum["maddeler"][0]
        print(f"ilk madde: {ilk_madde['baslik'][:70]} | {ilk_madde['url']}")

        d = await madde_getir(ilk_madde["url"])
        print(f"metin: {len(d['metin'])} karakter, ilk 80: {d['metin'][:80]!r}")
        assert d["metin"], "madde metni boş"

        try:
            await madde_getir("https://example.com/eskiler/2026/09/20260910-1.htm")
            raise AssertionError("yabancı host sessizce geçti")
        except ResmiGazeteHatasi:
            pass

        await kapat()

    asyncio.run(_dene())
    print("tamam")
