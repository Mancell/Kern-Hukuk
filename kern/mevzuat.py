"""
Mevzuat arama — UYAP Bedesten (kanun / yönetmelik / KHK / tebliğ …).

`ictihat.py`'nin ikizi: aynı `bedesten.adalet.gov.tr` arka ucu, aynı paylaşılan
hız kovası (bkz. bedesten.py). Fark, uç noktalar ve alan adları:
  - Arama   : POST /mevzuat/searchDocuments
  - İçerik  : POST /mevzuat/getDocumentContent  (documentType="MEVZUAT")

İçerik base64 HTML olarak geliyor. `<meta>` charset'i Windows-1254 diyor ama
baytlar gerçekte UTF-8 (yanlış etiket; canlı doğrulandı) — baytlara güveniyoruz.

Gizlilik sözleşmesi ictihat.py ile aynı: belge girmez, sorgu loglanmaz.

Tek başına çalışır:
    python server/mevzuat.py "türk borçlar"
"""
import asyncio
import base64
import re
import time

from bedesten import BedestenHatasi, CokHizli, MetneCevir, kapat, post

# Geriye dönük/tutarlı ad.
MevzuatHatasi = BedestenHatasi

__all__ = ["ara", "icerik", "madde", "madde_bul", "kapat", "MevzuatHatasi", "CokHizli", "TURLER"]

# Uygulamanın gönderdiği sade anahtar → Bedesten'in beklediği tür kodu. Kod
# telefona sızmıyor: karşı taraf değiştirirse sadece sunucu güncellenir.
TURLER = {
    "kanun": "KANUN",
    "khk": "KHK",
    "cbk": "CB_KARARNAME",           # Cumhurbaşkanlığı Kararnamesi
    "yonetmelik": "YONETMELIK",       # Bakanlar Kurulu / Kurum yönetmeliği
    "cb_yonetmelik": "CB_YONETMELIK",
    "teblig": "TEBLIGLER",
    "tuzuk": "TUZUK",
}

# ÖLÇÜLDÜ (2026-09-11): mevzuat ucunda üst sınır 10 — ve aşılınca HATA DÖNMÜYOR,
# sessizce `total: 0` + boş liste geliyor. `ictihat.py`'de aynı rakam 100'e
# çıkarıldığı (orada gerçekten çalışıyor) için buranın da "unutulmuş optimizasyon"
# sanılması çok kolay; denendi: pageSize 50/100/101 → hepsi 0 sonuç, aynı sorgu
# 10 ile 1632 sonuç veriyor. Büyütmeyin: arama sessizce tamamen boşalır.
SAYFA_BOYU = 10

# İçerik üst sınırı: TBK gibi kanunlar 1 MB+ metin. Telefonda tek `<Text>` içinde
# göstermek makul değil; ilk parçayı verip gerisini kaynağa yönlendiriyoruz.
_ICERIK_MAKS_KARAKTER = 200_000

# Ham metin upstream'e gitmeden önce süzülüyor (elle hazırlanmış istek riski).
_AD_BICIMI = re.compile(r"^[0-9A-Za-zçÇğĞıİöÖşŞüÜ.,()/ '\"*?-]{0,120}$")


def _kaynak_url(mevzuat_id: str) -> str:
    """Avukat kaynağı UYAP'ta doğrulayabilmeli."""
    return f"https://mevzuat.adalet.gov.tr/mevzuat/{mevzuat_id}"


def _tur_kodlari(tur: str | None) -> list[str]:
    """'' / None → tüm türler (browse). Aksi hâlde beyaz listeden tek tür."""
    if not tur:
        return list(TURLER.values())
    if tur not in TURLER:
        raise MevzuatHatasi("Bilinmeyen mevzuat türü.")
    return [TURLER[tur]]


async def ara(
    sorgu: str = "",
    tur: str | None = None,
    no: str | None = None,
    sayfa: int = 1,
    kimlik: str = "anon",
) -> dict:
    """
    Mevzuat araması.

    `sorgu` başlıkta aranır (mevzuatAdi); `no` doğrudan mevzuat numarası filtresi
    (ör. 6098 → Türk Borçlar Kanunu). İkisi de boşsa türe göre listeler (browse).
    """
    sorgu = (sorgu or "").strip()
    if len(sorgu) > 120 or not _AD_BICIMI.match(sorgu):
        raise MevzuatHatasi("Geçersiz arama metni.")
    no = (no or "").strip()
    if no and not no.isdigit():
        raise MevzuatHatasi("Mevzuat numarası yalnızca rakam olabilir.")
    sayfa = max(1, min(int(sayfa), 100))

    veri: dict = {
        "pageSize": SAYFA_BOYU,
        "pageNumber": sayfa,
        "mevzuatTurList": _tur_kodlari(tur),
        "mevzuatAdi": sorgu,
        "sortFields": ["RESMI_GAZETE_TARIHI"],
        "sortDirection": "desc",
    }
    if no:
        veri["mevzuatNo"] = no  # `kanunNo` DEĞİL — o alan yok sayılıyor (canlı doğrulandı)

    ham = await post(
        "/mevzuat/searchDocuments",
        {"data": veri, "applicationName": "UyapMevzuat", "paging": True},
        kimlik,
        onbelleklenebilir=True,
    )
    icerik_veri = ham.get("data") or {}
    kayitlar = [
        {
            "id": str(m.get("mevzuatId")),
            "no": m.get("mevzuatNo"),
            "ad": (m.get("mevzuatAdi") or "").strip(),
            "tur": (m.get("mevzuatTur") or {}).get("description") or "",
            "turKodu": (m.get("mevzuatTur") or {}).get("name") or "",
            "resmiGazeteTarihi": (m.get("resmiGazeteTarihi") or "")[:10],
            "resmiGazeteSayisi": m.get("resmiGazeteSayisi") or "",
            "url": m.get("url") or _kaynak_url(str(m.get("mevzuatId"))),
        }
        for m in icerik_veri.get("mevzuatList") or []
        if m.get("mevzuatId")
    ]
    return {
        "toplam": icerik_veri.get("total") or 0,
        "sayfa": sayfa,
        "tur": tur or "",
        "kayitlar": kayitlar,
    }


# Kanun metni herkes için aynı ve günlerce değişmez: çıkarılmış METİN 24 saat ortak önbellekte (yalnız SUNUCUNUN
# UYAP'tan çektiği metin girer — röle bu yolu kullanmaz). Bir kapsamlı dilekçenin UYAP isteklerinin yarıdan
# fazlası kanun maddesiydi; TBK/HMK artık günde bir kez çekilir. ponytail: süreç belleği, FIFO 40 kanun.
_METIN_TTL_SN = 24 * 3600
_METIN_KAPASITE = 40
_metinler: dict[str, tuple[float, str]] = {}
# Aynı anda istenen aynı kanun TEK indirilir (canlıda ajan 7 maddeyi paralel okuyunca TBK 7 kez çekilip UYAP
# sırasında 24 sn bekleniyordu).
_suren: dict[str, asyncio.Future] = {}


async def _tam_metin(mevzuat_id: str, kimlik: str) -> str:
    """Mevzuatın KIRPILMAMIŞ düz metni (24 saat önbellekli — madde aramaları tekrar indirmez)."""
    mevzuat_id = (mevzuat_id or "").strip()
    if not mevzuat_id.isdigit():
        raise MevzuatHatasi("Geçersiz mevzuat kimliği.")
    kayit = _metinler.get(mevzuat_id)
    if kayit and time.monotonic() - kayit[0] < _METIN_TTL_SN:
        return kayit[1]
    gorev = _suren.get(mevzuat_id)
    if gorev is None:
        gorev = _suren[mevzuat_id] = asyncio.ensure_future(_indir(mevzuat_id, kimlik))
        gorev.add_done_callback(lambda _: _suren.pop(mevzuat_id, None))
    return await asyncio.shield(gorev)  # bir bekleyenin iptali ortak indirmeyi düşürmesin


async def _indir(mevzuat_id: str, kimlik: str) -> str:
    ham = await post(
        "/mevzuat/getDocumentContent",
        {"data": {"id": mevzuat_id, "documentType": "MEVZUAT"}, "applicationName": "UyapMevzuat"},
        kimlik,
    )
    veri = ham.get("data") or {}
    if not veri.get("content"):
        raise MevzuatHatasi("Mevzuat metni alınamadı.")
    try:
        bayt = base64.b64decode(veri["content"])
    except Exception:
        raise MevzuatHatasi("Mevzuat metni çözülemedi.")

    tur = veri.get("mimeType") or ""
    if not tur.startswith("text/html"):
        raise MevzuatHatasi(f"Bu mevzuat desteklenmeyen bir biçimde geldi ({tur}).")

    # TUZAK: HTML `<meta>` charset'i Windows-1254 DİYOR ama baytlar gerçekte
    # UTF-8 (canlı doğrulandı: cp1254 çözümü 10.000+ mojibake üretiyor, utf-8
    # sıfır). Etikete değil baytlara güveniyoruz.
    cevirici = MetneCevir()
    cevirici.feed(bayt.decode("utf-8", "replace"))
    metin = cevirici.metin()
    _metinler.pop(mevzuat_id, None)
    if len(_metinler) >= _METIN_KAPASITE:
        _metinler.pop(next(iter(_metinler)))
    _metinler[mevzuat_id] = (time.monotonic(), metin)
    return metin


async def icerik(mevzuat_id: str, kimlik: str = "anon") -> dict:
    mevzuat_id = (mevzuat_id or "").strip()
    metin = await _tam_metin(mevzuat_id, kimlik)

    kirpildi = len(metin) > _ICERIK_MAKS_KARAKTER
    if kirpildi:
        metin = metin[:_ICERIK_MAKS_KARAKTER].rstrip() + "\n\n… (metin uzun, devamı için kaynağı açın)"

    return {"id": mevzuat_id, "metin": metin, "kirpildi": kirpildi, "kaynak": _kaynak_url(mevzuat_id)}


# Madde başlığı iki biçimde geliyor (ölçüldü): yeni kanunlar "MADDE 315-", eskiler "Madde 269 –";
# ek maddeler "MADDE 269/A-", "Madde 68/a –", "EK MADDE 1-", "GEÇİCİ MADDE 2-".
# Eski kanunlarda (İİK) "Madde" ile numara arasında SATIR SONU var: "Madde\n68 –" (ölçüldü).
_MADDE = re.compile(r"^[ \t]*((?:EK |Ek |GEÇİCİ |Geçici )?(?:MADDE|Madde)\s+(\d+(?:/[A-Za-zÇĞİÖŞÜçğıöşü]+)?))[ \t]*[-–—]", re.M)


def madde_bul(metin: str, no: str, tavan: int = 4000) -> str | None:
    """
    Metinden TEK maddeyi çıkarır (başlığından bir sonraki madde başlığına kadar). Hemen üstteki kısa
    kenar başlığı ("3. Kiracının temerrüdü") da eklenir. `no`: "315", "269/a", "ek 1", "geçici 2".
    Ağ yok — test edilebilir. Bulunamazsa None (çağıran UYDURMAZ).
    """
    hedef = re.sub(r"\s+", " ", (no or "").strip().lower())
    ek = hedef.startswith("ek ")
    gecici = hedef.startswith("geçici ") or hedef.startswith("gecici ")
    hedef = hedef.split(" ")[-1]
    eslesmeler = list(_MADDE.finditer(metin))
    for i, m in enumerate(eslesmeler):
        # Büyük harfle karşılaştır: "GEÇİCİ".lower() birleşen noktalı i üretir (CLAUDE.md "İ" tuzağı).
        bas = m.group(1).upper()
        if m.group(2).lower() != hedef or bas.startswith("EK ") != ek or bas.startswith("GE") != gecici:
            continue
        son = eslesmeler[i + 1].start() if i + 1 < len(eslesmeler) else len(metin)
        govde = metin[m.start():son].strip()
        onceki = metin[:m.start()].rstrip().rsplit("\n\n", 1)[-1].strip()
        if onceki and len(onceki) <= 90 and not onceki.endswith((".", ":", ";")) and not _MADDE.match(onceki):
            govde = f"{onceki}\n{govde}"
        return govde if len(govde) <= tavan else govde[:tavan].rstrip() + " …"
    return None


async def madde(mevzuat_id: str, no: str, kimlik: str = "anon") -> dict:
    """Bir mevzuatın TEK maddesinin tam metni — tüm kanunu modele vermeden doğrulama için."""
    metin = await _tam_metin(mevzuat_id, kimlik)
    govde = madde_bul(metin, no)
    if govde is None:
        raise MevzuatHatasi(f"{no}. madde bu mevzuatta bulunamadı.")
    return {"id": mevzuat_id, "madde": no, "metin": govde, "kaynak": _kaynak_url(mevzuat_id)}


# --------------------------------------------------------------------------
# Duman testi:  python server/mevzuat.py "türk borçlar"
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    async def _dene() -> None:
        # Tür haritası + doğrulama, ağ gerekmez.
        assert _tur_kodlari(None) == list(TURLER.values())
        ornek = (
            "Kira bedeli\n\nMADDE 314- Kiracı öder.\n\n3. Kiracının temerrüdü\n\nMADDE 315- Kiracı ifa etmezse.\n\n"
            "Kiracıya verilecek süre otuz gündür.\n\nMADDE 316- Özen.\n\nMadde 269 – Eski biçim.\n\n"
            "Madde\n269/a – Ek bent.\n\nGEÇİCİ MADDE 2- Geçiş.\n\nMADDE 2- Asıl iki."
        )
        g = madde_bul(ornek, "315")
        assert g and g.startswith("3. Kiracının temerrüdü\nMADDE 315-") and "otuz gündür" in g and "316" not in g, g
        assert madde_bul(ornek, "269").startswith("Madde 269 –")
        assert madde_bul(ornek, "269/A").startswith("Madde\n269/a –")  # İİK: numara alt satırda
        assert madde_bul(ornek, "geçici 2").startswith("GEÇİCİ MADDE 2-")
        assert madde_bul(ornek, "2").startswith("MADDE 2- Asıl")  # geçici 2 ile karışmaz
        assert madde_bul(ornek, "999") is None
        print("madde_bul: tamam")

        # Kanun metni önbelleği: ikinci çağrı UYAP'a gitmez.
        global post
        gercek, cagri = post, []

        async def sahte(yol, govde, kimlik="anon", onbelleklenebilir=False):
            cagri.append(yol)
            return {"data": {"mimeType": "text/html", "content": base64.b64encode("<p>MADDE 1- Deneme.</p>".encode()).decode()}}

        post = sahte
        try:
            a, b = await asyncio.gather(madde("999999", "1"), madde("999999", "1"))  # eşzamanlı → tek indirme
            assert a["metin"].startswith("MADDE 1-") and b == a
            assert (await madde("999999", "1"))["metin"].startswith("MADDE 1-")
            assert len(cagri) == 1, cagri
        finally:
            post = gercek
            _metinler.pop("999999", None)
        print("kanun metni önbelleği: tamam")
        assert _tur_kodlari("kanun") == ["KANUN"]
        try:
            _tur_kodlari("uydurma")
            raise AssertionError("bilinmeyen tür geçti")
        except MevzuatHatasi:
            pass
        for kotu in ("<script>", "x" * 200):
            try:
                await ara(kotu)
                raise AssertionError(f"geçersiz sorgu geçti: {kotu[:10]}")
            except MevzuatHatasi:
                pass
        try:
            await ara("kira", no="abc")
            raise AssertionError("harfli no geçti")
        except MevzuatHatasi:
            pass

        # Numara ile doğrudan bulma (6098 = TBK).
        s = await ara(no="6098")
        assert s["kayitlar"], "6098 bulunamadı"
        tbk = next((k for k in s["kayitlar"] if k["no"] == 6098), s["kayitlar"][0])
        print(f"no=6098 → {tbk['ad']} (id {tbk['id']})")

        # Başlık araması.
        q = sys.argv[1] if len(sys.argv) > 1 else "türk borçlar"
        s2 = await ara(q, tur="kanun")
        print(f"'{q}' (kanun) → {s2['toplam']} sonuç")
        assert s2["kayitlar"], "başlık araması boş döndü"

        # İçerik + cp1254 çözümü: Türkçe harf içermeli, bozulmamalı.
        ic = await icerik(tbk["id"])
        assert len(ic["metin"]) > 200, "içerik çok kısa"
        assert any(h in ic["metin"] for h in "çğıİöşü"), "cp1254 çözümü Türkçe harf içermiyor"
        assert "Ã" not in ic["metin"][:5000], "mojibake — charset yanlış çözülmüş olabilir"
        print(f"içerik: {len(ic['metin'])} karakter, kırpıldı={ic['kirpildi']}")
        print(ic["metin"][:300])
        await kapat()

    asyncio.run(_dene())
    print("\ntamam")
