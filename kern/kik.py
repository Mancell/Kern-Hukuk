"""
KİK (Kamu İhale Kurumu) Kurul Kararı arama — EKAP.

`aihm.py`/`literatur.py` kardeşi ama farklı bir yapıda: HUDOC/OpenAlex gibi
bir JSON API DEĞİL, klasik ASP.NET WebForms sayfası
(`ekap.kik.gov.tr/EKAP/Vatandas/KurulKararSorgu.aspx`). Arama bir
`__doPostBack` — önce taze bir GET ile gizli token'lar (`__EVENTVALIDATION`,
`__PIT`, `__PITC`) alınıyor, sonra aynı çerezlerle arama POST'u atılıyor.

SÖZLEŞME CANLI ÖLÇÜLDÜ (2026-09-14):
  - Zorunlu radyo alanı `kurulKararTip` BOŞ bırakılırsa arama metni SESSİZCE
    YOK SAYILIYOR: hem "aşırı düşük" hem alakasız bir ikinci metin, aynı
    "500" toplamı ve neredeyse birebir aynı karar listesini döndürdü — bu
    sessiz başarısızlık canlı ölçümle yakalandı, tahmin edilseydi fark
    edilmezdi. Radyo `rbUyusmazlik` (itirazen şikayet/uyuşmazlık kararları)
    ile dolduruldu: FARKLI serbest metinler GERÇEKTEN farklı karar listeleri
    döndürdü (liste sırası/içeriği metne göre değişti) — filtre bu haliyle
    çalışıyor.
  - Her arama KENDİ httpx istemcisini (ve dolayısıyla kendi çerez/oturum
    havuzunu) açıp kapatıyor — `aihm.py`'nin aksine PAYLAŞILAN bir istemci
    YOK. Sebep: ASP.NET oturum çerezi (`ASP.NET_SessionId`) istekler arası
    paylaşılırsa eşzamanlı iki kullanıcının GET/POST'ları birbirinin
    oturumuna karışabilir (yarış durumu). Bu aynı zamanda kullanıcının kendi
    isteği: "her kullanıcı kendi havuzundan çekebilmeli."

KARAR METNİ + GERÇEK KAYNAK BAĞLANTISI VAR (ilk ölçümde "yok" sanılmıştı —
`btnOnizle` postback'i href değil diye — ama postback'in KENDİSİ yanıtında
gizli bir iframe taşıyor: `KurulKararGoster.aspx?KararId=...&KararMetni=...`.
Bu URL çerezsiz/oturumsuz test edildi, HTTP 200 döndü — yani GERÇEK, kalıcı,
tek karara özgü bir bağlantı; avukat isterse tarayıcıda da açabilir. `detay()`
bu zinciri (GET → arama POST → Önizle POST → karar GET) kurup tam karar
metnini çekiyor — "kaynağa bağlantı" ilkesi burada da UYAP/HUDOC gibi tam
sağlanıyor, kullanıcı EKAP'ın boş arama sayfasına atılmıyor.

v1 kapsamı bilerek dar:
  - `ara()` yalnız SAYFA 1 (en güncel ~10 karar) döndürüyor. Sonraki sayfa
    ayrı bir postback ve ÖNCEKİ yanıttan alınan taze token gerektiriyor
    (klasik WebForms sayfalama) — tek istekte durumsuz kalan bu modelle
    uyuşmuyor; çok sayfalı gezinme kullanıcı başına kısa ömürlü oturum
    saklamak gerektirir, v2.
  - "Toplam Kayıt Sayısı" alanı GÜVENİLMEZ görünüyor (birbirinden çok farklı
    sorgularda hep "500" döndü — muhtemelen bir görüntüleme tavanı, gerçek
    eşleşme sayısı değil). Bu yüzden MODELDE YOK — yalnız o sayfadaki karar
    sayısı (`len(kayitlar)`) gösteriliyor, uydurma bir "toplam" verilmiyor.
  - `detay()` arama sonucunu YENİDEN KURUYOR (aynı sorgu/tur ile) ve `sira`
    (0 tabanlı, sayfadaki satır index'i) ile hedef satırın Önizle düğmesini
    postback'liyor. Arada sonuçlar değişmişse (yeni bir karar eklendi vs.)
    `sira` başka bir karara denk gelebilir — kullanıcıya "sonuçlar değişmiş
    olabilir" hatası dönülüyor, ASLA yanlış karar sessizce gösterilmiyor
    (aşağıdaki `karar_no` eşleşme kontrolü).

HIZ SINIRI (2026-09-14 ölçüldü): 20 art arda GET + 10 tam arama döngüsü
(GET+POST, `ara()` ile birebir aynı desen) EKAP'ta HİÇ 429/403/engel
tetiklemedi — Bedesten'in ~10 istek/30 sn'lik sert duvarının aksine burada
gözlenen bir sunucu-taraflı limit yok. Yine de `bedesten.py`'nin "adalet
kotası" desenini (`_Kova`/`CokHizli`) BURADA DA kuruyoruz: amaç EKAP'ın
limitini es geçmek değil, TEK kullanıcının (ya da bir betiğin) KİK'i sınırsız
yormasını engellemek — "her kullanıcı kendi havuzundan". Aralık (2 sn)
KASITLI GEVŞEK: bu zincirin kendi ağ gecikmesi (arama 3-5 sn, detay 4 istek
daha da uzun) zaten bundan büyük, yani NORMAL kullanımda kova HİÇ beklemeye
sebep olmuyor — yalnız art arda/otomatik istekleri yavaşlatıyor.

Gizlilik: yalnızca avukatın yazdığı arama metni EKAP'a gider (üçüncü taraf,
KİK — kamu kurumu). Loglanmaz, saklanmaz.

Tek başına çalışır:
    python server/kik.py "aşırı düşük"
"""
import asyncio
import html
import os
import re
import ssl

import httpx

from bedesten import CokHizli
from bedesten import _Kova as _KovaSinifi

BASE = "https://ekap.kik.gov.tr"

# Kullanıcı başına ADALET kotası — bedesten.py'deki `_kullanici_kovalari` ile
# AYNI mekanizma (kova sınıfı paylaşılıyor) ama KENDİ, ayrı sayacıyla; KİK'in
# EKAP kotası ictihat'ın UYAP kotasıyla karışmasın. Aralık gevşek tutuldu,
# yukarıdaki modül başlığına bakın.
_KULLANICI_ARALIK_SN = float(os.getenv("KIK_KULLANICI_ARALIK_SN", "2.0"))
_MAKS_BEKLEME_SN = float(os.getenv("KIK_MAKS_BEKLEME_SN", "8"))
_kullanici_kovalari: dict[str, _KovaSinifi] = {}


def _kullanici_kovasi(kimlik: str) -> _KovaSinifi:
    kova = _kullanici_kovalari.get(kimlik)
    if kova is None:
        kova = _KovaSinifi(_KULLANICI_ARALIK_SN)
        _kullanici_kovalari[kimlik] = kova
    return kova

# EKAP eski bir sunucu ciphersuite'i (TLSv1.2, RSA anahtar değişimli
# AES128-GCM-SHA256 — forward-secrecy YOK) sunuyor; OpenSSL 3.x'in varsayılan
# SECLEVEL=2'si bunu REDDEDİYOR ve el sıkışma `UNEXPECTED_EOF_WHILE_READING`
# ile sessizce çöküyor (canlı ölçüldü: curl/LibreSSL sorunsuz bağlanıyor,
# Python'un stdlib `ssl`'i SECLEVEL gevşetilmeden bağlanamıyor). Bu bot
# tespiti/insan taklidi DEĞİL, saf TLS uyumluluğu — sertifika doğrulaması
# AÇIK kalıyor, yalnızca izin verilen cipher seti genişletiliyor.
_TLS_BAGLAM = ssl.create_default_context()
_TLS_BAGLAM.set_ciphers("DEFAULT@SECLEVEL=0")
_YOL = "/EKAP/Vatandas/KurulKararSorgu.aspx"
_SORGU_MAKS = 200

_TUR_RADYO = {
    "uyusmazlik": "rbUyusmazlik",
    "duzenleyici": "rbDuzenleyici",
    "mahkeme": "rbMahkeme",
}

# Sayfanın kendi GET yanıtından ölçülen tam alan listesi (2026-09-14). Boş
# bırakılan alanlar sunucunun beklediği ama bizim doldurmadığımız serbest
# süzgeçler — dolu göndermek arama SONUCUNU değil kapsamı daraltıyor.
_BOS_ALANLAR = {
    "__EVENTARGUMENT": "",
    "__LASTFOCUS": "",
    "__SCROLLPOSITIONX": "0",
    "__SCROLLPOSITIONY": "0",
    "ctl00$Menu1$hdnAktIKN": "",
    "ctl00$ContentPlaceHolder1$txtKararNo": "",
    "ctl00$ContentPlaceHolder1$etKararTarihBaslangic$EkapTakvimTextBox_etKararTarihBaslangic": "",
    "ctl00$ContentPlaceHolder1$etKararTarihBitis$EkapTakvimTextBox_etKararTarihBitis": "",
    "ctl00$ContentPlaceHolder1$txtResmiGazeteSayisi": "",
    "ctl00$ContentPlaceHolder1$etResmiGazeteTarihi$EkapTakvimTextBox_etResmiGazeteTarihi": "",
    "ctl00$ContentPlaceHolder1$txtBasvuruKonusuIhale": "",
    "ctl00$ContentPlaceHolder1$txtSikayetci": "",
    "ctl00$ContentPlaceHolder1$txtIhaleyiYapanIdare": "",
    "ctl00$ContentPlaceHolder1$ddlYil": "0",
}
_GIZLI_ALAN_ADLARI = (
    "__VIEWSTATE", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION", "__PIT", "__PITC",
)
# Satırdaki span id → çıktı alanı. Karar tipine göre kolonlar değişiyor
# (uyuşmazlık: İdare var, Gündem Konusu yok — ölçüldü); ikisi de kapsanıyor.
_ALAN_ADI = {
    "lblKno": "karar_no",
    "lblKtar": "karar_tarihi",
    "lblIdare": "idare",
    "lblSikayetci": "basvuru_sahibi",
    "lblIhale": "ihale",
    "lblBaskonu": "konu",
}


class KikHatasi(Exception):
    """Arama başarısız. Çağıran kullanıcıya gösterilebilir mesaja çevirir."""


def _sorgu_temizle(sorgu: str) -> str:
    if len(sorgu) > _SORGU_MAKS:
        raise KikHatasi("Arama metni çok uzun.")
    temiz = re.sub(r"[\x00-\x1f\x7f]", " ", sorgu).strip()
    return re.sub(r"\s+", " ", temiz)


def _gizli_alanlar(sayfa_html: str) -> dict:
    alanlar = {}
    for ad in _GIZLI_ALAN_ADLARI:
        m = re.search(rf'id="{ad}" value="([^"]*)"', sayfa_html)
        alanlar[ad] = m.group(1) if m else ""
    return alanlar


def _metin(deger: str | None) -> str | None:
    if deger is None:
        return None
    return html.unescape(deger).strip() or None


def _satirlari_ayikla(sayfa_html: str) -> list[dict]:
    parcalar = re.split(r'(?=id="lblKno")', sayfa_html)
    kayitlar = []
    for parca in parcalar[1:]:
        blok = parca[:2000]
        alanlar = dict(re.findall(r'id="(lbl\w+)" title="([^"]*)"', blok))
        if "lblKno" not in alanlar:
            continue
        kayit = {cikti: _metin(alanlar.get(span)) for span, cikti in _ALAN_ADI.items()}
        kayitlar.append(kayit)
    return kayitlar


def _metne_cevir(parca_html: str) -> str:
    """Karar metni EKAP'tan zengin bir iç içe HTML belgesi olarak geliyor
    (kendi <head>/<style>'ı olan bir Word-dışa-aktarım kalıbı) — burada
    düz metne indiriliyor."""
    parca_html = re.sub(r"<head\b[^>]*>.*?</head>", " ", parca_html, flags=re.I | re.S)
    parca_html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", parca_html, flags=re.I | re.S)
    parca_html = re.sub(r"<(br|p|div|tr|li|h[1-6])\b[^>]*/?>", "\n", parca_html, flags=re.I)
    parca_html = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", parca_html, flags=re.I)
    metin = re.sub(r"<[^>]+>", "", parca_html)
    metin = html.unescape(metin)
    metin = re.sub(r"[ \t]+", " ", metin)
    metin = re.sub(r"\n[ \t]+", "\n", metin)
    metin = re.sub(r"\n{3,}", "\n\n", metin)
    return metin.strip()


def _karar_metnini_ayikla(sayfa_html: str) -> str:
    m = re.search(r'id="ctl00_ContentPlaceHolder1_lblKarar"', sayfa_html)
    if not m:
        raise KikHatasi("Karar metni sayfada bulunamadı.")
    baslangic = sayfa_html.find(">", m.end()) + 1
    bitis = sayfa_html.find("</html>", baslangic)
    govde = sayfa_html[baslangic : bitis + len("</html>")] if bitis != -1 else sayfa_html[baslangic:]
    return _metne_cevir(govde)


async def _tekrarli(istek, hata_mesaji: str, deneme: int = 2) -> httpx.Response:
    """EKAP ara sıra bağlantıyı sessizce düşürüyor (2026-09-15 ölçüldü: ~3
    denemeden 1'i `ConnectError`) — tek bir ağ isteğini bu yüzden bir kez
    tekrar dener. `istek` yan etkisiz tekrarlanabilir bir GET/POST olmalı."""
    for kalan in range(deneme, 0, -1):
        try:
            return await istek()
        except httpx.RequestError:
            if kalan == 1:
                raise KikHatasi(hata_mesaji)
            await asyncio.sleep(0.5)


async def _arama_yap(istemci: httpx.AsyncClient, temiz: str, radyo: str) -> httpx.Response:
    get_yanit = await _tekrarli(lambda: istemci.get(_YOL), "KİK (EKAP) servisine ulaşılamadı.")
    if get_yanit.status_code != 200:
        raise KikHatasi(f"EKAP sayfası açılamadı (HTTP {get_yanit.status_code}).")

    govde = {
        **_BOS_ALANLAR,
        **_gizli_alanlar(get_yanit.text),
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$btnAra",
        "ctl00$ContentPlaceHolder1$kurulKararTip": radyo,
        "ctl00$ContentPlaceHolder1$txtKararMetni": temiz,
    }
    post_yanit = await _tekrarli(
        lambda: istemci.post(_YOL, data=govde), "KİK (EKAP) araması başarısız — sunucuya ulaşılamadı."
    )
    if post_yanit.status_code != 200:
        raise KikHatasi(f"KİK araması başarısız (HTTP {post_yanit.status_code}).")
    return post_yanit


def _istemci() -> httpx.AsyncClient:
    # Her arama/detay KENDİ istemcisini (çerez oturumunu) açar — paylaşılan
    # bir istemci eşzamanlı kullanıcıların ASP.NET oturumunu karıştırırdı;
    # bkz. modül başlığı ("her kullanıcı kendi havuzundan").
    return httpx.AsyncClient(
        base_url=BASE,
        timeout=20.0,
        follow_redirects=True,
        verify=_TLS_BAGLAM,
        headers={"User-Agent": "Mozilla/5.0 (compatible; mancel-kik/1.0)"},
    )


async def ara(sorgu: str = "", tur: str = "uyusmazlik", kimlik: str = "anon") -> dict:
    """KİK Kurul Kararı araması (EKAP). `tur`: uyusmazlik/duzenleyici/mahkeme.

    `kimlik` imza simetrisi + her aramanın kendi izole httpx istemcisini/
    çerez oturumunu açması için var — "her kullanıcı kendi havuzundan".
    """
    temiz = _sorgu_temizle((sorgu or "").strip())
    radyo = _TUR_RADYO.get(tur, _TUR_RADYO["uyusmazlik"])
    if kimlik != "anon":
        await _kullanici_kovasi(kimlik).jeton(_MAKS_BEKLEME_SN)

    async with _istemci() as istemci:
        post_yanit = await _arama_yap(istemci, temiz, radyo)

    return {
        "sayfa": 1,
        "kaynak": "KİK (EKAP)",
        "kayitlar": _satirlari_ayikla(post_yanit.text),
    }


async def detay(
    sorgu: str = "", tur: str = "uyusmazlik", sira: int = 0,
    karar_no: str = "", kimlik: str = "anon",
) -> dict:
    """`ara()`'nın döndürdüğü `sira`. (0 tabanlı) satırın TAM karar metnini
    getirir. Aramayı YENİDEN KURUP aynı listeden hedef satırın Önizle
    düğmesini postback'liyor — sonuçlar arada değişmişse (yeni karar
    eklenmiş vs.) `karar_no` uyuşmazlığı yakalar, ASLA yanlış kararı sessizce
    döndürmez.
    """
    if sira < 0 or sira > 9:
        raise KikHatasi("Geçersiz karar sırası.")
    temiz = _sorgu_temizle((sorgu or "").strip())
    radyo = _TUR_RADYO.get(tur, _TUR_RADYO["uyusmazlik"])
    if kimlik != "anon":
        await _kullanici_kovasi(kimlik).jeton(_MAKS_BEKLEME_SN)

    async with _istemci() as istemci:
        arama_yanit = await _arama_yap(istemci, temiz, radyo)
        satirlar = _satirlari_ayikla(arama_yanit.text)
        if sira >= len(satirlar):
            raise KikHatasi("Karar artık bulunamadı — sonuçlar değişmiş olabilir, aramayı tekrarlayın.")
        hedef = satirlar[sira]
        if karar_no and hedef.get("karar_no") != karar_no:
            raise KikHatasi("Sonuçlar değişmiş — aramayı tekrarlayıp tekrar deneyin.")

        onizle_govde = {
            **_BOS_ALANLAR,
            **_gizli_alanlar(arama_yanit.text),
            "__EVENTTARGET": f"ctl00$ContentPlaceHolder1$grdKurulKararSorguSonuc$ctl{3 + sira:02d}$btnOnizle",
            "ctl00$ContentPlaceHolder1$kurulKararTip": radyo,
            "ctl00$ContentPlaceHolder1$txtKararMetni": temiz,
        }
        onizle_yanit = await _tekrarli(
            lambda: istemci.post(_YOL, data=onizle_govde), "KİK önizleme isteği başarısız."
        )
        if onizle_yanit.status_code != 200:
            raise KikHatasi(f"KİK önizleme başarısız (HTTP {onizle_yanit.status_code}).")

        m = re.search(r'id="iframe_detayPopUp" src="([^"]+)"', onizle_yanit.text)
        if not m:
            raise KikHatasi("Karar önizlemesi bulunamadı.")
        karar_yolu = html.unescape(m.group(1))
        karar_tam_yol = f"{_YOL.rsplit('/', 1)[0]}/{karar_yolu}"

        karar_yanit = await _tekrarli(lambda: istemci.get(karar_tam_yol), "Karar sayfası açılamadı.")
        if karar_yanit.status_code != 200:
            raise KikHatasi(f"Karar sayfası açılamadı (HTTP {karar_yanit.status_code}).")

    return {
        "karar_no": hedef["karar_no"],
        "metin": _karar_metnini_ayikla(karar_yanit.text),
        "kaynak_url": f"{BASE}{karar_tam_yol}",
    }


# --------------------------------------------------------------------------
# Duman testi:  python server/kik.py "aşırı düşük"
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    assert _sorgu_temizle("  aşırı   düşük  ") == "aşırı düşük"
    ornek = (
        '<tr><td><a id="btnOnizle" href="javascript:__doPostBack(...)"></a></td>'
        '<td><span id="lblKno" title="2026/UH.II-2322">2026/UH.II-2322</span></td>'
        '<td><span id="lblKtar" title="02.09.2026">02.09.2026</span></td>'
        '<td><span id="lblIdare" title="X &quot;Genel&quot; Müdürlüğü">X "Genel" Müdürlüğü</span></td>'
        '<td><span id="lblSikayetci" title="Y Ltd.">Y Ltd.</span></td>'
        '<td><span id="lblIhale" title="2026/1 İhalesi">2026/1 İhalesi</span></td></tr>'
    )
    satirlar = _satirlari_ayikla(ornek)
    assert len(satirlar) == 1
    k = satirlar[0]
    assert k["karar_no"] == "2026/UH.II-2322" and k["karar_tarihi"] == "02.09.2026"
    assert k["idare"] == 'X "Genel" Müdürlüğü' and k["konu"] is None
    ic = (
        '<span id="ctl00_ContentPlaceHolder1_lblKarar">'
        '<head><style>.x{color:red}</style></head><body><p>Birinci satır.</p>'
        '<p>İkinci &amp; satır.</p></body></html></span>'
    )
    assert _karar_metnini_ayikla(ic) == "Birinci satır.\n\nİkinci & satır."
    print("birim: tamam")

    async def _dene() -> None:
        q = " ".join(sys.argv[1:]) or "aşırı düşük"
        s = await ara(q)
        print(f"'{q}' → {len(s['kayitlar'])} karar (sayfa 1)")
        assert s["kayitlar"], "sonuç boş döndü"
        ilk = s["kayitlar"][0]
        print(f"ilk: {ilk['karar_no']} | {ilk['karar_tarihi']} | {ilk['basvuru_sahibi']}")
        assert ilk["karar_no"], "karar no yok"

        d = await detay(q, sira=0, karar_no=ilk["karar_no"])
        print(f"detay: {d['karar_no']} | {len(d['metin'])} karakter | {d['kaynak_url']}")
        assert d["karar_no"] == ilk["karar_no"] and d["metin"] and d["kaynak_url"].startswith(BASE)

        try:
            await detay(q, sira=0, karar_no="olmayan-bir-karar-no")
            raise AssertionError("uyuşmayan karar_no sessizce geçti")
        except KikHatasi:
            pass

    asyncio.run(_dene())
    print("tamam")
