"""
Doğal dil → Bedesten arama sorgusu.

Avukat "kiracı evi boşaltmıyor ne yapabilirim" yazıyor; model bunu
`"tahliye davası" +kira` gibi bir ARAMA SORGUSUNA çeviriyor. Arama yine UYAP'ta
çalışıyor, sonuçlar yine UYAP'tan geliyor.

NEDEN BU KULLANIM CLAUDE.md'DEKİ "İÇTİHATTA ÜRETKEN MODEL YOK" İLKESİNİ BOZMUYOR:
model hukuki içerik üretmiyor, yalnızca arama kutusuna yazılacak metni öneriyor.
Uydurulmuş bir esas/karar numarası imkânsız çünkü model sonuçlara hiç dokunmuyor;
gördüğü tek şey avukatın kendi cümlesi. Öneri ekranda DÜZENLENEBİLİR gösteriliyor,
son sözü avukat söylüyor.

Model kodu bilerek `ictihat.py`'nin DIŞINDA: arama yolunda model olmadığı
`grep -i nvidia server/ictihat.py` ile doğrulanabilir kalsın diye.

Model çağrısı `llm.py` üzerinden (sağlayıcı/model tek yerde). Bir model ölürse
(bkz. llama-3.3'ün 2026-08-26'da emekliye ayrılması) düzeltme kod değil env.

Duman testi (gerçek API'ye gider, LLM anahtarı ister):
    GEMINI_API_KEY=... python server/sorgu_oneri.py "kiracı evi boşaltmıyor"
"""
import re
from collections import OrderedDict

import kalite
import llm

MAKS_GIRDI = 300
MAKS_ONERI = 200


class OneriYok(Exception):
    """Öneri üretilemedi. Çağıran ham sorguyla devam etmeli — arama çalışmaya devam eder."""


_SISTEM = """Sen bir Türk hukuk arama asistanısın. Görevin, avukatın günlük dille
yazdığı hukuki durumu UYAP Bedesten içtihat arama sorgusuna çevirmek.

Sorgu dili kuralları:
- "çift tırnak" içindeki kelimeler o sırayla ve bitişik aranır
- +kelime o kelimenin mutlaka geçmesini zorunlu kılar
- -kelime o kelimeyi içeren kararları eler
- Joker karakter (*) ÇALIŞMAZ

İyi bir sorgu, hukuk terimlerini kullanır ve dar tutar. Günlük dildeki kelimeleri
("ev sahibi", "boşaltmıyor") hukuki karşılıklarıyla ("kiraya veren", "tahliye")
değiştir.

YALNIZCA sorguyu yaz. Açıklama, tırnak dışında noktalama, satır başı, giriş
cümlesi YAZMA. En fazla 12 kelime.

Örnekler:
kiracı evi boşaltmıyor ne yapabilirim -> "tahliye davası" +kira
komşum balkondan bakıyor gizliliğimi ihlal ediyor -> "özel hayatın gizliliği" +komşu
işten çıkarıldım tazminat alabilir miyim -> "işe iade" +kıdem +ihbar
trafik kazasında araç değer kaybı -> "değer kaybı" +trafik"""

# Kabul edilen karakterler: Türkçe harfler, rakam, boşluk ve sorgu dili işaretleri.
# Modelin markdown, kod bloğu, satır başı, emoji veya URL döndürmesi ihtimaline karşı.
# Parantez BİLEREK yok. `(` ve `)` izinliyken denge kontrol edilmiyordu ve
# `"kira" +(tahliye` gibi bir çıktı süzgeci geçiyordu; `_operator_var_mi` onu
# "avukat kendi yazmış" sayıp Bedesten'e aynen yolluyor, orada ayrıştırma hatası
# 400'e dönüşüyor ve avukat "servis yanıt vermedi" görüp sebebini anlamıyordu —
# yani `dogrula`'nın önlemek için var olduğu durum. Sistem istemi de parantezden
# hiç bahsetmiyor, dolayısıyla kaybedilen işlev yok.
_IZINLI = re.compile(r'^[0-9A-Za-zçÇğĞıİöÖşŞüÜâÂîÎûÛ "+\-]+$')
_HARF = re.compile(r"[A-Za-zçÇğĞıİöÖşŞüÜ]")


def dogrula(ham: str) -> str | None:
    """
    Modelin çıktısını süzer. Kabul edilebilir değilse None.

    Model çıktısı asla doğrudan kullanılmıyor. Sebebi güvenlik değil isabet:
    talimatları yok sayıp "Tabii, işte sorgunuz:" diye başlayan ya da markdown
    kod bloğu döndüren bir yanıt, Bedesten'e olduğu gibi giderse arama çöp döner
    ve avukat sebebini anlamaz.
    """
    if not ham:
        return None
    oneri = ham.strip().strip("`").strip()
    # Tek satır: model bazen açıklama satırı ekliyor, ilkini alıyoruz.
    oneri = oneri.splitlines()[0].strip() if oneri.splitlines() else ""
    if not oneri or len(oneri) > MAKS_ONERI:
        return None
    if not _IZINLI.match(oneri):
        return None
    if not _HARF.search(oneri):
        return None
    if oneri.count('"') % 2:
        return None  # dengesiz tırnak Bedesten'de sorguyu bozar
    return oneri


# Aynı cümleye aynı öneri: ücretsiz LLM kotası (günde ~20 istek) tekrar için harcanmasın.
_ONBELLEK: "OrderedDict[str, str]" = OrderedDict()
_ONBELLEK_MAKS = 500


def _modelsiz(metin: str) -> str | None:
    """Çevrilecek bir CÜMLE yoksa modele gitmeden sorgu: zaten sorgu dili kullanılmışsa
    (tırnak/+/-) aynen, en fazla 2 kelimelik bir terimse tırnaklı öbek. Diğer her şey None."""
    if any(c in metin for c in '"+') or re.search(r"(^|\s)-\w", metin):
        return dogrula(metin)
    kelimeler = metin.split()
    if len(kelimeler) <= 2:
        return dogrula(f'"{" ".join(kelimeler)}"' if len(kelimeler) == 2 else kelimeler[0])
    return None


async def oner(metin: str) -> str:
    """Doğal dil metnini Bedesten sorgusuna çevirir. Başarısızsa OneriYok."""
    metin = (metin or "").strip()
    if not metin:
        raise OneriYok("Ne aradığınızı yazın.")
    if len(metin) > MAKS_GIRDI:
        raise OneriYok("Metin çok uzun.")
    if hazir := _modelsiz(metin):
        return hazir
    anahtar = " ".join(metin.lower().split())
    if anahtar in _ONBELLEK:
        _ONBELLEK.move_to_end(anahtar)
        return _ONBELLEK[anahtar]
    if not llm.hazir():
        raise OneriYok("Sorgu önerisi bu sunucuda kapalı.")

    try:
        # Yalnız ana model (kullanıcı kararı "her yerde DeepSeek"); yoğunsa ≤20 sn beklenir — arama önerisi
        # etkileşimli. Kesik yanıt önbelleğe GİRMEZ.
        ham, kesik = await kalite.ana_ile(lambda: llm.yaz(
            [
                {"role": "system", "content": _SISTEM},
                {"role": "user", "content": metin},
            ],
            max_tokens=800,
            temperature=0.2,     # sorgu üretimi yaratıcılık işi değil
            timeout=30.0,
        ), en_fazla=20.0)
    except (kalite.KaliteHatasi, llm.LLMHatasi):
        raise OneriYok("Öneri üretilemedi.")
    if kesik:
        raise OneriYok("Öneri üretilemedi.")

    oneri = dogrula(ham)
    if oneri is None:
        raise OneriYok("Öneri üretilemedi.")
    _ONBELLEK[anahtar] = oneri
    if len(_ONBELLEK) > _ONBELLEK_MAKS:
        _ONBELLEK.popitem(last=False)
    return oneri


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    # Süzgeç ağ olmadan doğrulanabilmeli — asıl kırılgan yer burası.
    assert dogrula('"tahliye davası" +kira') == '"tahliye davası" +kira'
    assert dogrula('  "kira tespiti"  ') == '"kira tespiti"'
    assert dogrula('```\n"kira tespiti"\n```') == '"kira tespiti"', "kod bloğu soyulmalı"
    assert dogrula('"kira tespiti"\nBu sorgu kira...') == '"kira tespiti"', "açıklama atılmalı"
    assert dogrula('"dengesiz tırnak') is None, "tek tırnak reddedilmeli"
    assert dogrula("") is None
    assert dogrula("   ") is None
    assert dogrula("+++") is None, "harf içermeyen reddedilmeli"
    assert dogrula("x" * 300) is None, "uzun reddedilmeli"
    assert dogrula("bkz: https://ornek.com") is None, "URL reddedilmeli"
    # Modelsiz kısa yol: terim/sorgu zaten hazırsa kota harcanmaz; cümle modele gider.
    assert _modelsiz("kira tespiti") == '"kira tespiti"'
    assert _modelsiz("tahliye") == "tahliye"
    assert _modelsiz('"işe iade" +kıdem') == '"işe iade" +kıdem'
    assert _modelsiz("kira -ceza") == "kira -ceza"
    assert _modelsiz("kiracı evi boşaltmıyor") is None
    assert _modelsiz("ev sahibi-kiracı uyuşmazlığı var") is None, "tire içeren cümle modele gitmeli"
    assert dogrula("SELECT * FROM kararlar;") is None, "noktalı virgül/yıldız reddedilmeli"
    assert dogrula("kira 🏠 tespiti") is None, "emoji reddedilmeli"
    assert dogrula("işçi-işveren +tazminat") == "işçi-işveren +tazminat", "tire geçerli"
    # Parantez: denge kontrol edilmediği için tamamen reddediliyor.
    assert dogrula('"kira" +(tahliye') is None, "dengesiz parantez reddedilmeli"
    assert dogrula("kira ((( tespiti") is None
    assert dogrula("(kira OR tahliye) +tespit") is None, "dengeli olsa da parantez yok"
    print("süzgeç: 16 vaka tamam")

    if not llm.hazir():
        print("LLM anahtarı yok — canlı çağrı atlandı.")
        raise SystemExit(0)

    async def _dene() -> None:
        for m in (sys.argv[1:] or [
            "kiracı evi boşaltmıyor ne yapabilirim",
            "işten çıkarıldım tazminat alabilir miyim",
            "trafik kazasında aracımda değer kaybı oluştu",
        ]):
            try:
                print(f"  {m!r}\n    -> {await oner(m)}")
            except OneriYok as e:
                print(f"  {m!r}\n    -> BAŞARISIZ: {e}")

    asyncio.run(_dene())
