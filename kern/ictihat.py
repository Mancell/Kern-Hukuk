"""
İçtihat arama — UYAP Bedesten (emsal karar).

Kaynak: `bedesten.adalet.gov.tr`, yani `mevzuat.adalet.gov.tr`'nin arka ucu.
İstek/yanıt şekli saidsurucu/yargi-mcp (MIT) çözümlemesinden alındı; paketin
kendisi bağımlılık olarak eklenmedi çünkü markitdown+fastmcp+openai+numpy
ağacını dönüştürme imajına sokuyor ve bize o ağacın %2'si lazım.

Altyapı (HTTP istemcisi, hız kovası, HTML→metin, önbellek) `bedesten.py`'de:
`mevzuat.py` ile AYNI çıkış IP'sini paylaştığımız için hız kovası da paylaşılmak
zorunda (bkz. bedesten.py). Bu dosyada yalnızca emsal karara özgü mantık kaldı.

Gizlilik sözleşmesi (app.py'deki ile aynı):
  - Kullanıcının BELGESİ bu uçlara hiç girmez; dışarı çıkan tek şey arama sorgusu.
  - Sorgu metni hiçbir log satırına yazılmaz. Bilerek tek bir logger çağrısı yok.

Bu dosya expo/react bilmez ve tek başına çalışır:
    python server/ictihat.py "kira tespiti"
"""
import asyncio
import base64
import os
import re

import role

from bedesten import (
    BASLIKLAR,
    TABAN,
    BedestenHatasi,
    CokHizli,
    MetneCevir,
    kapat,
    post,
)

# Geriye dönük ad: app.py ve eski çağrı yerleri `ictihat.IctihatHatasi` bekliyor.
IctihatHatasi = BedestenHatasi

__all__ = [
    "ara", "karar", "kapat", "sorguyu_kur", "IctihatHatasi", "CokHizli", "BedestenHatasi",
    # Doğrudan erişim: telefon Bedesten'e KENDİ IP'sinden gider, sunucu yalnız
    # sorguyu kurar (plan_kur) ve ham yanıtı çözer (aramayi_normalize/karar_coz).
    "plan_kur", "plan_kur_karar", "aramayi_normalize", "karari_normalize", "karar_coz",
]

# Uygulamanın gönderdiği sade anahtar → Bedesten'in beklediği tür kodu.
# Kodları telefona sızdırmıyoruz ki karşı taraf değiştirdiğinde sadece sunucu
# güncellensin, kurulu uygulamalar kırılmasın.
MAHKEMELER = {
    "yargitay": "YARGITAYKARARI",
    "danistay": "DANISTAYKARAR",
    "istinaf": "ISTINAFHUKUK",
    "yerel": "YERELHUKUK",
    "kyb": "KYB",
    # Bedesten'de VAR ama eklenmemişti; canlı doğrulandı (ANAYASA 1.510,
    # UYUSMAZLIK 3.411 sonuç). Ceza karşılıkları (YERELCEZA/ISTINAFCEZA) ve
    # SAYISTAY bu serviste YOK — 0 sonuç dönüyor, ayrı sistemler.
    "anayasa": "ANAYASA",
    "uyusmazlik": "UYUSMAZLIK",
}

# Bedesten'in GERÇEK üst sınırı 100 (101 istenince "must be less than or equal
# to 100" hatası döner — ölçüldü). Buradaki değer 10'du ve yorumu "üst sınır,
# büyütmek işe yaramıyor" diyordu; yanlıştı. 10 ile aynı 100 sonucu gezmek için
# kovadan 10 jeton gidiyordu, şimdi 1. Hız sınırı bu özelliğin en kıt kaynağı
# olduğu için buradaki tek rakam, proxy/önbellek tarafındaki tüm uğraştan daha
# çok kazandırıyor.
SAYFA_BOYU = 100

# Sorgu dili işaretleri. Biri varsa avukat ne yaptığını biliyordur, sorguya
# dokunmayız — ama tespit DAR olmak zorunda, çünkü yanlış pozitif doğrudan
# VEYA aramasına düşmek demek (aşağıdaki `sorguyu_kur` açıklamasına bakın).
#
# Önceki hâli `"-" in sorgu` ve `" NOT " in sorgu.upper()` diyordu. İkisi de
# sıradan Türkçe sorguları operatör sanıyordu:
#   "işçi-işveren uyuşmazlığı"  → tire yüzünden
#   "2019-2020 dönemi kira"     → tire yüzünden
#   "not tebliği"               → "NOT" Türkçe bir kelime
# Üçü de tırnaklanmadan gidiyordu, yani fonksiyonun önlemek için var olduğu
# tam hataya düşüyordu.
_TIRNAK = '"'
# +/- yalnızca kelime BAŞINDA operatör (Lucene sözdizimi); ortadaki tire normal.
_ONEK = re.compile(r"(?:^|\s)[+-]\S")
# AND/OR/NOT yalnızca BÜYÜK harfle ve tam kelime. Küçük harfe bakmıyoruz:
# "not", "or" Türkçede/kelime içinde geçebiliyor.
_MANTIK = re.compile(r"(?:^|\s)(?:AND|OR|NOT)(?:\s|$)")


def _operator_var_mi(sorgu: str) -> bool:
    return bool(_TIRNAK in sorgu or _ONEK.search(sorgu) or _MANTIK.search(sorgu))


# --------------------------------------------------------------------------
# Arama
# --------------------------------------------------------------------------

def sorguyu_kur(sorgu: str, kip: str) -> str:
    """
    Ham kullanıcı metnini Bedesten sorgusuna çevirir.

    BU FONKSİYON ÖZELLİĞİN TAMAMI KADAR ÖNEMLİ. Bedesten boşlukla ayrılmış
    kelimeleri VEYA sayıyor: `kira tespiti` 2.2 milyon karar döndürüyor ve ilk
    sırada 6136 sayılı Kanun'a aykırılıktan ceza kararları çıkıyor. Aynı arama
    `"kira tespiti"` olarak 2.092 karar ve hepsi 3. Hukuk Dairesi.
    Tırnaklamayı kaldıran, özelliği çöpe atar.
    """
    if _operator_var_mi(sorgu):
        return sorgu  # avukat kendi sorgusunu yazmış, karışmıyoruz
    kelimeler = sorgu.split()
    if len(kelimeler) < 2:
        return sorgu
    if kip == "tumu":
        return " ".join(f"+{k}" for k in kelimeler)
    return f'"{sorgu}"'


# Daire adı Bedesten'e TAM TÜRKÇE adla gidiyor ("3. Hukuk Dairesi"). Kısaltma
# tablosu (H1–H23, C1–C23, HGK, D1–D17…) BİLEREK yok: adlar zaten arama
# sonuçlarında `birimAdi` olarak geliyor ve istemci onları aynen geri gönderiyor.
# Tablo tutmak, karşı taraf bir daire adını değiştirdiğinde iki yerde bakım demek.
# Yine de ham metin upstream'e gitmeden önce süzülüyor.
_DAIRE_BICIMI = re.compile(r"^[0-9A-Za-zçÇğĞıİöÖşŞüÜ. ]{1,60}$")

# ISO 8601 tarihi. Doğrulanmadan geçirilirse 10 MB'lık bir "tarih" paylaşılan tek
# çıkış IP'sinden Bedesten'e gider ve UYAP o IP'yi engellerse özellik herkes için
# ölür — mobil uygulama bu alanları göndermese bile elle hazırlanmış istek gönderir.
_TARIH_BICIMI = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:.]{1,12}Z?)?$")


def _tarihi_tamamla(t: str | None) -> str | None:
    """
    Bedesten TAM zaman damgası istiyor; `2015-01-01` gönderilince eşleşme
    bulmuyor ve SESSİZCE 0 sonuç dönüyor — filtrenin hiç olmamasından kötü,
    çünkü avukat "bu yıl karar yok" sanıyor. Ölçüldü: `2015-01-01` → 0 karar,
    `2015-01-01T00:00:00.000Z` → 184 karar.
    """
    if not t:
        return None
    return t if "T" in t else f"{t}T00:00:00.000Z"


def _dogrula(
    sorgu: str, mahkeme: str, sayfa: int, baslangic: str | None, bitis: str | None,
    kip: str, daire: str | None,
) -> tuple[str, int]:
    """
    Girdi doğrulama — `ara()` ve `plan_kur()` ORTAK. Sıra bilinçli, hata mesajları
    dış sözleşme. Temizlenmiş `(sorgu, sayfa)` döner.
    """
    sorgu = (sorgu or "").strip()
    if not sorgu:
        raise IctihatHatasi("Arama kutusu boş.")
    if len(sorgu) > 300:
        raise IctihatHatasi("Arama metni çok uzun.")
    if mahkeme not in MAHKEMELER:
        raise IctihatHatasi("Bilinmeyen mahkeme türü.")
    if kip not in ("ifade", "tumu"):
        raise IctihatHatasi("Bilinmeyen arama kipi.")
    if daire and not _DAIRE_BICIMI.match(daire):
        raise IctihatHatasi("Geçersiz daire.")
    for t in (baslangic, bitis):
        if t and not _TARIH_BICIMI.match(t):
            raise IctihatHatasi("Geçersiz tarih.")
    return sorgu, max(1, min(int(sayfa), 100))


def _govde_kur(
    sorgu: str, mahkeme: str, sayfa: int, baslangic: str | None, bitis: str | None,
    kip: str, daire: str | None,
) -> dict:
    """Bedesten `data` sözlüğü — `_ara_bir` ve `plan_kur` aynı şeyi göndersin diye tek yer."""
    veri: dict = {
        "pageSize": SAYFA_BOYU,
        "pageNumber": sayfa,
        "itemTypeList": [MAHKEMELER[mahkeme]],
        "phrase": sorguyu_kur(sorgu, kip),
        # `sortDirection: "asc"` Bedesten tarafında YOK SAYILIYOR (test edildi) —
        # tarih sıralaması her hâlükârda yeniden eskiye. Boş `sortFields` başka
        # bir sıra veriyor ama alaka sırası olduğu doğrulanamadı, o yüzden
        # tahmine dayalı bir "alaka" vaat etmiyoruz.
        "sortFields": ["KARAR_TARIHI"],
        "sortDirection": "desc",
    }
    if daire:
        veri["birimAdi"] = daire
    if bas := _tarihi_tamamla(baslangic):
        veri["kararTarihiStart"] = bas
    if bit := _tarihi_tamamla(bitis):
        veri["kararTarihiEnd"] = bit
    return veri


def _zarfla(veri: dict) -> dict:
    """Tel üstünde giden tam gövde. İstemci bunu AYNEN gönderir, sunucu da öyle."""
    return {"data": veri, "applicationName": "UyapMevzuat", "paging": True}


async def ara(
    sorgu: str,
    mahkeme: str = "yargitay",
    sayfa: int = 1,
    baslangic: str | None = None,
    bitis: str | None = None,
    kip: str = "ifade",
    daire: str | None = None,
    kimlik: str = "anon",
) -> dict:
    """
    Serbest metin içtihat araması.

    `kip`:
      "ifade" — kelimeler bir arada geçsin (varsayılan; en isabetli)
      "tumu"  — kelimelerin hepsi geçsin, sırası önemsiz (daha geniş)
    Sorguda tırnak / +/- / AND-OR-NOT varsa kip yok sayılır, sorgu aynen gider.

    "ifade" hiç sonuç vermezse kendiliğinden "tumu"ya düşer; dönen `kip` alanı
    hangisinin kullanıldığını söyler, uygulama bunu kullanıcıya yazar.

    `daire` alakayı ciddi daraltıyor — ölçüldü: `"kira tespiti"` filtresiz 2.092,
    3. Hukuk Dairesi 798, 6. Hukuk Dairesi 731, Hukuk Genel Kurulu 65.
    """
    sorgu, sayfa = _dogrula(sorgu, mahkeme, sayfa, baslangic, bitis, kip, daire)

    sonuc = await _ara_bir(sorgu, mahkeme, sayfa, baslangic, bitis, kip, daire, kimlik)
    # Tam ifade tutmadıysa avukatı boş ekranla baş başa bırakmıyoruz.
    #
    # Ama geri düşüş jeton maliyetini İKİYE katlıyor ve jeton bu özelliğin en
    # kıt kaynağı. Sorgu iki kipte de AYNI metne dönüşüyorsa (tek kelime, ya da
    # avukat zaten kendi operatörlerini yazmış) ikinci istek birebir aynı olurdu:
    # aynı boş sonuç için iki jeton. O yüzden sadece gerçekten farklıysa deniyoruz.
    if (
        not sonuc["kararlar"]
        and kip == "ifade"
        and sayfa == 1
        and sorguyu_kur(sorgu, "tumu") != sorguyu_kur(sorgu, "ifade")
    ):
        try:
            genis = await _ara_bir(sorgu, mahkeme, sayfa, baslangic, bitis, "tumu", daire, kimlik)
        except CokHizli:
            # Geri düşüş bir KOLAYLIK. Jetonu alamadıysak elimizdeki geçerli (boş)
            # sonucu döndürüyoruz. Eskiden `CokHizli` dışarı fırlayıp 429'a
            # dönüşüyordu: avukat "Sonuç bulunamadı" yerine "Arama servisi yoğun"
            # görüyor, tekrar deniyor ve kovayı daha da yiyordu.
            return sonuc
        if genis["kararlar"]:
            return genis
    return sonuc


async def _ara_bir(
    sorgu: str, mahkeme: str, sayfa: int, baslangic: str | None, bitis: str | None,
    kip: str, daire: str | None = None, kimlik: str = "anon",
) -> dict:
    veri = _govde_kur(sorgu, mahkeme, sayfa, baslangic, bitis, kip, daire)
    ham = await post("/emsal-karar/searchDocuments", _zarfla(veri), kimlik, onbelleklenebilir=True)
    return aramayi_normalize(ham, veri, kip)


def aramayi_normalize(ham: object, veri: dict, kip: str) -> dict:
    """
    Ham Bedesten arama yanıtı → uygulamanın beklediği şekil. `_ara_bir` ve
    `/ictihat/coz` ORTAK: `sayfa`/`kullanilanSorgu`/`daire` yerel değişkenden
    değil `veri`den (gönderilen `data` sözlüğünden) okunur ki iki yol bayt-aynı
    çıktı versin.

    `ham` istemciden de gelebilir (doğrudan erişim) — dict değilse ya da
    beklenen alanlar dict değilse sessizce boş sayılır, AttributeError yok.
    """
    icerik = (ham.get("data") if isinstance(ham, dict) else None) or {}
    if not isinstance(icerik, dict):
        icerik = {}
    kararlar = [
        {
            "id": k.get("documentId"),
            "daire": k.get("birimAdi") or "",
            "esasNo": k.get("esasNo") or "",
            "kararNo": k.get("kararNo") or "",
            "tarih": k.get("kararTarihiStr") or "",
            "mahkeme": (k.get("itemType") or {}).get("description") or "",
        }
        for k in icerik.get("emsalKararList") or []
        if isinstance(k, dict) and k.get("documentId")
    ]
    return {
        "toplam": icerik.get("total") or 0,   # anahtar var ama None gelebiliyor
        "sayfa": veri.get("pageNumber", 1),
        "kip": kip,
        "kullanilanSorgu": veri.get("phrase", ""),
        "daire": veri.get("birimAdi") or "",
        "kararlar": kararlar,
    }


# --------------------------------------------------------------------------
# Karar metni
# --------------------------------------------------------------------------
# Kararlar değişmez, dolayısıyla önbellek her zaman doğru. Asıl faydası
# hız değil hız SINIRI: aynı emsale bakan ikinci avukat kovadan jeton yemiyor.
# ponytail: süreç belleğinde sade FIFO. Yeniden başlatınca boşalır, yeter.
_ONBELLEK: dict[str, str] = {}
_ONBELLEK_KAPASITE = int(os.getenv("ICTIHAT_ONBELLEK", "200"))


def _pdf_metni(bayt: bytes) -> str:
    import pymupdf as fitz  # dönüştürme tarafında zaten kurulu; düz `import fitz` uyarı basıyor

    with fitz.open(stream=bayt, filetype="pdf") as belge:
        return "\n".join(sayfa.get_text() for sayfa in belge).strip()


def _karar_id(belge_id: str) -> str:
    belge_id = (belge_id or "").strip()
    if not belge_id.isdigit():
        raise IctihatHatasi("Geçersiz karar kimliği.")
    return belge_id


def _karar_govdesi(belge_id: str) -> dict:
    return {"data": {"documentId": belge_id}, "applicationName": "UyapMevzuat"}


async def karari_normalize(ham: object) -> str:
    """
    Ham getDocumentContent yanıtı → düz metin. `karar()` ve `/ictihat/coz` ORTAK.
    PDF dalı pymupdf ister — telefonda yok; `/coz`'un var olma sebebi bu.
    async: PDF çıkarımı `to_thread`.
    """
    veri = (ham.get("data") if isinstance(ham, dict) else None) or {}
    if not isinstance(veri, dict) or not veri.get("content"):
        raise IctihatHatasi("Karar metni alınamadı.")
    try:
        bayt = base64.b64decode(veri["content"])
    except Exception:
        raise IctihatHatasi("Karar metni çözülemedi.")

    tur = veri.get("mimeType") or ""
    if not isinstance(tur, str):
        tur = ""
    if tur.startswith("text/html"):
        cevirici = MetneCevir()
        cevirici.feed(bayt.decode("utf-8", "replace"))
        return cevirici.metin()
    if tur == "application/pdf":
        return await asyncio.to_thread(_pdf_metni, bayt)
    raise IctihatHatasi(f"Bu karar desteklenmeyen bir biçimde geldi ({tur}).")


async def karar_coz(belge_id: str, ham: object) -> dict:
    """
    /ictihat/coz için: istemcinin KENDİ IP'sinden çektiği ham karar yanıtını çözer.

    KIRMIZI ÇİZGİ — `_ONBELLEK`'E YAZMAZ. İstemciden gelen karar metni ortak
    önbelleğe girse, bir kullanıcı uydurma esas/karar numarasını diğer avukatlara
    servis ettirebilir (CLAUDE.md: uydurma künye = meslek riski). Çıktı yalnız
    çağırana döner.
    """
    belge_id = _karar_id(belge_id)
    return {"id": belge_id, "metin": await karari_normalize(ham), "kaynak": _kaynak_url(belge_id)}


async def karar(belge_id: str, kimlik: str = "anon") -> dict:
    belge_id = _karar_id(belge_id)

    onbellekten = _ONBELLEK.get(belge_id)
    if onbellekten is not None:
        return {"id": belge_id, "metin": onbellekten, "kaynak": _kaynak_url(belge_id)}

    ham = await post("/emsal-karar/getDocumentContent", _karar_govdesi(belge_id), kimlik)
    metin = await karari_normalize(ham)

    # Röle açıkken yanıt telefondan gelmiş olabilir → ortak önbelleğe girmez (karar_coz ile aynı kırmızı çizgi).
    if role.aktif.get() is not None:
        return {"id": belge_id, "metin": metin, "kaynak": _kaynak_url(belge_id)}
    if len(_ONBELLEK) >= _ONBELLEK_KAPASITE:
        _ONBELLEK.pop(next(iter(_ONBELLEK)))
    _ONBELLEK[belge_id] = metin
    return {"id": belge_id, "metin": metin, "kaynak": _kaynak_url(belge_id)}


def _kaynak_url(belge_id: str) -> str:
    """Avukat kaynağı tarayıcıda doğrulayabilmeli — 'yapay zekâ uydurdu mu' sorusunun cevabı."""
    return f"https://mevzuat.adalet.gov.tr/ictihat/{belge_id}"


# --------------------------------------------------------------------------
# Doğrudan erişim planı
# --------------------------------------------------------------------------
# Telefon, Bedesten'e KENDİ IP'sinden gider; UYAP'ın per-IP sınırı telefon
# başına uygulanır, sunucunun tek çıkış IP'si (ve `_kovalar`) hiç kullanılmaz.
# Sunucu yine de sorguyu kurar ve doğrular — `sorguyu_kur` bu özelliğin kalbi
# (tırnaksız "kira tespiti" 2,2 milyon alakasız karar) ve istemciye gömülürse
# UYAP formatı değiştiğinde 100 kişinin uygulama güncellemesi beklenir.
#
# Bu fonksiyonlar HİÇ ağa çıkmaz, kova ve önbelleğe dokunmaz.


def plan_kur(
    sorgu: str,
    mahkeme: str = "yargitay",
    sayfa: int = 1,
    baslangic: str | None = None,
    bitis: str | None = None,
    kip: str = "ifade",
    daire: str | None = None,
) -> dict:
    """
    `ara()` ile AYNI doğrulama ve AYNI gövde; farkı isteği göndermek yerine
    istemciye vermek. `istekler` sırayla denenir: `ara()`nın "ifade boşsa
    tumu'ya düş" davranışının aynası olarak, koşul tutuyorsa ikinci plan da
    eklenir; istemci ilki boş dönerse ikincisini dener.
    """
    sorgu, sayfa = _dogrula(sorgu, mahkeme, sayfa, baslangic, bitis, kip, daire)
    istekler = [{"kip": kip, "govde": _zarfla(_govde_kur(sorgu, mahkeme, sayfa, baslangic, bitis, kip, daire))}]
    if kip == "ifade" and sayfa == 1 and sorguyu_kur(sorgu, "tumu") != sorguyu_kur(sorgu, "ifade"):
        istekler.append(
            {"kip": "tumu", "govde": _zarfla(_govde_kur(sorgu, mahkeme, sayfa, baslangic, bitis, "tumu", daire))}
        )
    return {"url": TABAN + "/emsal-karar/searchDocuments", "basliklar": dict(BASLIKLAR), "istekler": istekler}


def plan_kur_karar(belge_id: str) -> dict:
    belge_id = _karar_id(belge_id)
    return {
        "url": TABAN + "/emsal-karar/getDocumentContent",
        "basliklar": dict(BASLIKLAR),
        "istekler": [{"kip": "", "govde": _karar_govdesi(belge_id)}],
    }


# --------------------------------------------------------------------------
# Duman testi:  python server/ictihat.py "kira tespiti"
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    async def _dene() -> None:
        # Sorgu kurulumu — ağ gerekmez, ama özelliğin isabeti tamamen buna bağlı.
        assert sorguyu_kur("kira tespiti", "ifade") == '"kira tespiti"'
        assert sorguyu_kur("kira tespiti", "tumu") == "+kira +tespiti"
        assert sorguyu_kur("tahliye", "ifade") == "tahliye", "tek kelime tırnaklanmamalı"
        assert sorguyu_kur('"kira tespiti"', "tumu") == '"kira tespiti"', "tırnaklıya dokunma"
        assert sorguyu_kur("kira AND tespit", "ifade") == "kira AND tespit", "operatöre dokunma"
        assert sorguyu_kur("+kira tespit", "ifade") == "+kira tespit", "önekli artıya dokunma"
        assert sorguyu_kur("kira -tespit", "ifade") == "kira -tespit", "önekli eksiye dokunma"

        # Türkçe sorguları operatör sanma. Bunların hepsi eskiden tırnaksız
        # gidiyordu, yani doğrudan VEYA aramasına (milyonlarca alakasız karar).
        assert sorguyu_kur("işçi-işveren uyuşmazlığı", "ifade") == '"işçi-işveren uyuşmazlığı"', \
            "kelime ORTASINDAKİ tire operatör değil"
        assert sorguyu_kur("2019-2020 dönemi kira", "ifade") == '"2019-2020 dönemi kira"', \
            "tarih aralığı operatör değil"
        assert sorguyu_kur("not tebliği", "ifade") == '"not tebliği"', \
            "'not' Türkçe bir kelime, NOT operatörü değil"
        assert sorguyu_kur("kira and tespit", "ifade") == '"kira and tespit"', \
            "küçük harf 'and' operatör değil"

        for bos in ("", "   "):
            try:
                await ara(bos)
                raise AssertionError("boş sorgu geçti")
            except IctihatHatasi:
                pass

        # Geçersiz daire/tarih upstream'e ulaşmadan reddedilmeli.
        for kotu in ({"daire": "x" * 200}, {"daire": "<script>"}, {"baslangic": "y" * 5000}):
            try:
                await ara("kira", **kotu)
                raise AssertionError(f"geçersiz parametre geçti: {list(kotu)[0]}")
            except IctihatHatasi:
                pass

        # --- Doğrudan erişim: plan + çözümleme, ağ gerekmez -------------------
        pl = plan_kur("kira tespiti")
        assert pl["url"].endswith("/emsal-karar/searchDocuments")
        assert [i["kip"] for i in pl["istekler"]] == ["ifade", "tumu"], "ifade boşsa tumu planı da olmalı"
        assert pl["istekler"][0]["govde"]["data"]["phrase"] == '"kira tespiti"'
        assert pl["istekler"][1]["govde"]["data"]["phrase"] == "+kira +tespiti"
        assert pl["istekler"][0]["govde"]["applicationName"] == "UyapMevzuat"
        assert "User-Agent" in pl["basliklar"] and "Origin" in pl["basliklar"]
        assert len(plan_kur("tahliye")["istekler"]) == 1, "tek kelime: iki kip aynı, tek plan"
        assert len(plan_kur("kira tespiti", sayfa=2)["istekler"]) == 1, "sayfa 2'de geri düşüş yok"
        assert len(plan_kur("kira tespiti", kip="tumu")["istekler"]) == 1
        for kotu in ({"sorgu": "x" * 301}, {"sorgu": "kira", "mahkeme": "yok"}, {"sorgu": "kira", "daire": "<script>"}):
            try:
                plan_kur(**kotu)
                raise AssertionError(f"plan_kur geçersiz girdiyi geçirdi: {kotu}")
            except IctihatHatasi:
                pass
        try:
            plan_kur_karar("12a")
            raise AssertionError("plan_kur_karar sayı olmayan kimliği geçirdi")
        except IctihatHatasi:
            pass
        assert plan_kur_karar(" 42 ")["istekler"][0]["govde"]["data"]["documentId"] == "42"

        veri_ornek = {"pageNumber": 3, "phrase": '"kira tespiti"', "birimAdi": "3. Hukuk Dairesi"}
        ham_ornek = {"data": {"total": None, "emsalKararList": [
            {"documentId": "1", "birimAdi": "3. Hukuk Dairesi", "esasNo": "2020/1", "kararNo": "2021/2",
             "kararTarihiStr": "01.01.2021", "itemType": {"description": "Yargıtay Kararı"}},
            {"birimAdi": "kimliksiz — atlanmalı"},
            "dict değil — atlanmalı",
        ]}}
        n = aramayi_normalize(ham_ornek, veri_ornek, "ifade")
        assert n["toplam"] == 0, "total None → 0"
        assert n["sayfa"] == 3 and n["kullanilanSorgu"] == '"kira tespiti"' and n["daire"] == "3. Hukuk Dairesi", \
            "echo alanları veri'den gelmeli"
        assert len(n["kararlar"]) == 1 and n["kararlar"][0] == {
            "id": "1", "daire": "3. Hukuk Dairesi", "esasNo": "2020/1", "kararNo": "2021/2",
            "tarih": "01.01.2021", "mahkeme": "Yargıtay Kararı"}
        assert aramayi_normalize("çöp", {}, "ifade")["kararlar"] == [], "dict olmayan ham → boş, hata yok"
        assert aramayi_normalize({"data": {"emsalKararList": "çöp"}}, {}, "ifade")["kararlar"] == []

        html_b64 = base64.b64encode("<b>8. Ceza</b><br>Son".encode()).decode()
        assert await karari_normalize({"data": {"mimeType": "text/html", "content": html_b64}}) == "8. Ceza\nSon"
        kc = await karar_coz("7", {"data": {"mimeType": "text/html", "content": html_b64}})
        assert kc == {"id": "7", "metin": "8. Ceza\nSon", "kaynak": "https://mevzuat.adalet.gov.tr/ictihat/7"}
        assert "7" not in _ONBELLEK, "karar_coz ORTAK ÖNBELLEĞE YAZMAMALI (zehirlenme kırmızı çizgisi)"
        for kotu_ham in (
            {"data": {"mimeType": "image/png", "content": html_b64}},
            {"data": {"mimeType": "text/html"}},
            {"data": {"mimeType": "text/html", "content": "%%%bozuk"}},
            "çöp",
        ):
            try:
                await karari_normalize(kotu_ham)
                raise AssertionError(f"karari_normalize kötü girdiyi geçirdi: {kotu_ham!r:.40}")
            except IctihatHatasi:
                pass

        sonuc = await ara(sys.argv[1] if len(sys.argv) > 1 else "kira tespiti")
        assert sonuc["kararlar"], "hiç sonuç yok"
        print(f"{sonuc['toplam']} karar ({sonuc['kullanilanSorgu']}), kip={sonuc['kip']}")

        # Daire filtresi alakayı daraltıyor mu?
        d = sonuc["kararlar"][0]["daire"]
        dar = await ara(sys.argv[1] if len(sys.argv) > 1 else "kira tespiti", daire=d)
        gelen = {k["daire"] for k in dar["kararlar"]}
        assert gelen <= {d}, f"filtre tutmadı: {gelen}"
        print(f"daire={d!r} → {dar['toplam']} karar (filtresiz {sonuc['toplam']})")

        # Tarih aralığı
        assert _tarihi_tamamla("2015-01-01") == "2015-01-01T00:00:00.000Z"
        assert _tarihi_tamamla("2015-01-01T00:00:00.000Z") == "2015-01-01T00:00:00.000Z"
        assert _tarihi_tamamla(None) is None

        ilk = await karar(sonuc["kararlar"][0]["id"])
        assert len(ilk["metin"]) > 50, ilk["metin"]
        assert await karar(sonuc["kararlar"][0]["id"]) == ilk, "önbellek tutarsız"
        print(f"\n--- {ilk['kaynak']} ---\n{ilk['metin'][:400]}")
        await kapat()

    asyncio.run(_dene())
    print("\ntamam")
