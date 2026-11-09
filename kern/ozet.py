"""
Karar / mevzuat ÖZETİ — yapay zeka.

Kullanıcı bir kararı veya mevzuatı açıp "Yapay Zeka Özeti" isteyince, o belgenin
GERÇEK metnini (`ictihat.karar` / `mevzuat.icerik`) alıp `llm.py` ile özetler.
Özet YALNIZCA verilen gerçek metne dayanır; esas/karar/madde numarası UYAP'tan
geldiği gibidir, model uydurmaz. Cihazda özetin yanında "AI üretimi, doğrulayın"
uyarısı gösterilir.

DEĞİŞMEZ #2 (güncellendi): model artık içtihat/mevzuat SONUÇ METNİNİ görüyor ve
özetliyor — ama yalnızca UYAP'ın döndürdüğü gerçek metni, künye uydurmadan. Bu
kod bilerek `ictihat.py`'nin DIŞINDA: `grep -iE "nvidia|gemini|llm" server/ictihat.py`
boş kalmaya devam eder (denetlenebilirlik).

Duman testi (canlı, LLM anahtarı + gerçek belge id ister):
    GEMINI_API_KEY=... python server/ozet.py karar <belge_id>
"""
import asyncio
import re
from collections import OrderedDict

import ictihat
import kalite
import llm
import mevzuat

# Kalite önce (2026-10-23): metin artık sessizce 24.000'de KESİLMEZ.
#   Karar: baş + son birlikte (hüküm sonda) — `_kirp_karar`.
#   Kanun/belge: tek parçaya sığmıyorsa parça parça ara not → tek özet (`_parcala`).
MAKS_METIN = 40_000      # tek çağrıda gönderilen en uzun metin
PARCA_KRK = 30_000       # uzun metinde parça boyu
MAKS_PARCA = 6           # 6 × 30.000 = 180.000 karakter; ötesi yazılı notla belirtilir
KARAR_BAS, KARAR_SON = 26_000, 14_000
MAKS_OZET_TOKEN = 1600   # alt başlıklar yer kaplıyor
MAKS_OZET_TOKEN_UST = 4000  # kesilirse bir kez bununla yeniden
# Özet uçları akışsız (istemci ~2 dk bekler): ana model yoğunsa bu kadar beklenir, yedek modele GEÇİLMEZ.
ANA_BEKLEME_SN = 90.0
OZET_SURUMU = "v2"  # önbellek anahtarında: denetimsiz eski özetler servis edilmesin


# Karar/mevzuat özeti ORTAK önbelleği: belge değişmez, aynı özeti herkes için yeniden
# üretmek ücretsiz LLM kotasını (günde ~20 istek, model başına) boşa harcıyordu.
# Herkese ortak olabilir çünkü metni SUNUCU UYAP'tan çekiyor (`ictihat.karar`,
# `mevzuat.icerik`). İSTEMCİDEN gelen metin (belge özeti, /ictihat/coz) buraya ASLA
# girmez — biri uydurma metni başkalarına "özet" diye servis ettiremesin.
_ONBELLEK: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
_ONBELLEK_MAKS = 300


def onbellekten(tur: str, kimlik_no: str) -> dict | None:
    """Hazır özet (varsa). Çağıran isabette kota saymadan döndürebilir."""
    k = (f"{tur}:{OZET_SURUMU}", kimlik_no)
    if k in _ONBELLEK:
        _ONBELLEK.move_to_end(k)
        return _ONBELLEK[k]
    return None


def _sakla(tur: str, kimlik_no: str, sonuc: dict, tam: bool = True) -> dict:
    """Yalnız TAM özet önbelleğe girer (kesik/kısaltılmış özet herkese servis edilmesin)."""
    if not tam:
        return sonuc
    _ONBELLEK[(f"{tur}:{OZET_SURUMU}", kimlik_no)] = sonuc
    while len(_ONBELLEK) > _ONBELLEK_MAKS:
        _ONBELLEK.popitem(last=False)
    return sonuc


class OzetHatasi(Exception):
    """Özet üretilemedi. Kullanıcıya gösterilir."""


# Biçim (2026-10-04, kullanıcı isteği): sabit ALT BAŞLIKLAR (### …) + künyeler
# **kalın**. Uygulama özeti `components/Markdown.tsx` ile çiziyor. Metinde karşılığı
# olmayan başlık YAZILMAZ — boş başlık altına bilgi uydurmaya davet olurdu.
_BICIM = """BİÇİM: Aşağıdaki alt başlıkları AYNEN "### " ile yaz, her birinin altında
"- " ile kısa maddeler. Metinde karşılığı olmayan başlığı tamamen ATLA. Kanun adı ve
numarası, madde numarası, esas/karar numarası, tarih ve tutarları **kalın** yaz —
yalnız metinde geçtiği gibi. Başka markdown (tablo, # tek başlık) kullanma."""

_KURAL = """KURALLAR: YALNIZCA verilen metne dayan. Metinde OLMAYAN bir bilgi, kanun,
madde numarası, esas/karar numarası veya sonuç EKLEME. Emin olmadığın yeri yazma."""

_SISTEM_KARAR = f"""Sen Türk hukukunda uzman bir asistansın. Aşağıda bir mahkeme
kararının TAM METNİ var. Bir hukukçu için özetle.

{_BICIM}
### Künye
(mahkeme/daire, esas ve karar numarası, karar tarihi)
### Uyuşmazlık
(taraflar ne istiyor)
### Gerekçe
(mahkeme neden böyle karar verdi)
### Sonuç / Hüküm
### Dayanılan Kanun ve Maddeler

{_KURAL}"""

_SISTEM_MEVZUAT = f"""Sen Türk hukukunda uzman bir asistansın. Aşağıda bir mevzuat
metni (kanun/yönetmelik/tebliğ vb.) var. Bir hukukçu için özetle.

{_BICIM}
### Künye
(mevzuat adı, numarası, Resmî Gazete tarihi/sayısı — metinde geçiyorsa)
### Konu ve Kapsam
### Önemli Hükümler
(her maddede ilgili **madde numarası**)
### Atıf Yapılan Kanunlar
### Dikkat Edilecek Noktalar

{_KURAL}"""

# Belge özeti başına KÜNYE satırları (2026-10-19, "Belge araçları" tasarımı): uygulama bunları ayrıştırıp
# künye kartı + süre kartı çiziyor (src/udf/ozetRaporu.ts); eski kurulumlar düz paragraf olarak okur.
# SÜRE'de son gün HESAPLATILMAZ — istemci `sureSonGunu` ile hesaplar (tek kaynak, CLAUDE.md).
_SISTEM_BELGE = f"""Sen Türk hukukunda uzman bir asistansın. Aşağıda bir hukuki
belgenin (UYAP evrakı, dilekçe, karar, sözleşme vb.) düz metni var. Bir hukukçu
için özetle.

ÖNCE şu künye satırlarını AYNEN bu etiketlerle, her biri tek satır yaz; metinde karşılığı
olmayan satırı tamamen ATLA (uydurma):
TÜR: <belge türü BÜYÜK HARFLE, ör. BİLİRKİŞİ RAPORU>
KONU: <tek cümlelik konu>
MAHKEME: <mahkeme/merci>
DOSYA: <esas/dosya numarası, metinde geçtiği gibi>
TARİH: <belgenin tarihi, metinde geçtiği gibi>
TARAFLAR: <taraflar, "–" ile>
SÜRE: <işlem, ör. Rapora itiraz> | <sürenin başladığı tarih GG.AA.YYYY, metinde AÇIKÇA yazıyorsa; yoksa boş bırak> | <gün sayısı, sayı> | <dayanak, ör. HMK m. 281>
(SÜRE satırını YALNIZ metinde bir süre açıkça yazıyorsa yaz. Gün sayısını metinden al; hafta/ay ile yazılmışsa
güne çevir: "bir hafta" = 7, "iki hafta" = 14, "bir ay" = 30. Karar ve tebligatta kanun yolu süresi (istinaf, temyiz,
itiraz) yazıyorsa bu satırı ATLAMA — "tebliğden itibaren" ise başlangıç tarihini boş bırak. Son günü HESAPLAMA.)

Sonra bölümleri yaz. Her bölümde maddeleri ÖNEM SIRASIYLA yaz (en önemli iki madde başta —
kısa görünümde yalnız onlar gösterilir).

{_BICIM}
### Konu / Talep
### Önemli Tarih, Tutar ve Numaralar
(esas/karar numaraları metinde geçtiği gibi)
### Dayanılan Kanun ve Maddeler
### Sonuç ve Dikkat Edilecekler

{_KURAL}"""


# --------------------------------------------------------------------------
# Üretim: yalnız ana model, kesik algılama, uzun metin
# --------------------------------------------------------------------------
_SISTEM_ARA = """Sen Türk hukukunda uzman bir asistansın. Aşağıdaki metin daha uzun bir belgenin {i}/{n}. PARÇASI.
Bu parçadaki önemli noktaları "- " ile madde madde çıkar: taraflar, talepler, olgular, madde numaraları, esas/karar
numaraları, tarihler, tutarlar, süreler, sonuç/hüküm. Sayı ve tarihleri metinde geçtiği gibi yaz. Yorum ekleme,
metinde olmayanı yazma."""


async def _uret(sistem: str, metin: str, butce: int = MAKS_OZET_TOKEN) -> tuple[str, bool]:
    """(özet, tam). Kesilirse bir kez daha büyük bütçeyle; yine kesikse son tam satıra kadar alınır."""
    mesajlar = [{"role": "system", "content": sistem}, {"role": "user", "content": metin}]

    async def cagri(b: int):
        return await kalite.ana_ile(
            lambda: llm.yaz(mesajlar, max_tokens=b, temperature=0.2, timeout=60.0), en_fazla=ANA_BEKLEME_SN)

    try:
        cikti, kesik = await cagri(butce)
        if kesik:
            cikti, kesik = await cagri(MAKS_OZET_TOKEN_UST)
    except kalite.KaliteHatasi as e:
        raise OzetHatasi(str(e))
    if kesik:
        cikti = "\n".join(cikti.splitlines()[:-1])
    return cikti.strip(), not kesik


def _kirp_karar(metin: str) -> tuple[str, bool]:
    """Uzun kararın başı + sonu (hüküm sonda). (metin, kisaltildi)"""
    if len(metin) <= MAKS_METIN:
        return metin, False
    return metin[:KARAR_BAS] + "\n\n[… kararın orta kısmı uzunluk nedeniyle çıkarıldı …]\n\n" + metin[-KARAR_SON:], True


def _parcala(metin: str) -> list[str]:
    """Paragraf sınırında ~PARCA_KRK'lik parçalar."""
    parcalar, bas = [], 0
    while bas < len(metin):
        son = min(len(metin), bas + PARCA_KRK)
        if son < len(metin):
            kes = metin.rfind("\n", bas + PARCA_KRK // 2, son)
            son = kes if kes > 0 else son
        parcalar.append(metin[bas:son])
        bas = son
    return parcalar


async def _ozetle(metin: str, sistem: str, tur: str = "belge") -> tuple[str, bool]:
    """(denetlenmiş özet, tam). Tam = kesilmedi ve metnin tamamı okundu."""
    metin = (metin or "").strip()
    if not metin:
        raise OzetHatasi("Belgede özetlenecek metin bulunamadı.")
    tam_okundu = True
    if tur == "karar":
        girdi, kisaltildi = _kirp_karar(metin)
        tam_okundu = not kisaltildi
    elif len(metin) <= MAKS_METIN:
        girdi = metin
    else:
        parcalar = _parcala(metin)
        if len(parcalar) > MAKS_PARCA:
            parcalar, tam_okundu = parcalar[:MAKS_PARCA], False
        notlar = await asyncio.gather(*(
            _uret(_SISTEM_ARA.format(i=i, n=len(parcalar)), p) for i, p in enumerate(parcalar, 1)))
        girdi = "BELGENİN PARÇA PARÇA ÇIKARILMIŞ NOTLARI (sırayla):\n\n" + "\n\n".join(
            f"[Parça {i}]\n{n}" for i, (n, _t) in enumerate(notlar, 1))
        tam_okundu = tam_okundu and all(t for _n, t in notlar)
    ozet, tam = await _uret(sistem, girdi)
    ozet = dogrula(ozet, metin)
    if not tam_okundu:
        ozet += "\n### Not\n- Metin çok uzun olduğu için yalnız ilk bölümü özetlendi; tamamını kontrol edin."
    return ozet, tam and tam_okundu


# --------------------------------------------------------------------------
# Kod düzeyinde doğrulama: özetteki tarih / numara / tutar KAYNAK METİNDE geçmeli
# --------------------------------------------------------------------------
_AYLAR = {a: i for i, a in enumerate(["ocak", "şubat", "mart", "nisan", "mayıs", "haziran", "temmuz", "ağustos",
                                      "eylül", "ekim", "kasım", "aralık"], 1)}
_TARIH_RE = re.compile(r"\b(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})\b")
_TARIH_YAZI_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_AYLAR) + r")\s+(\d{4})\b")
_NO_RE = re.compile(r"\b(\d{4})\s*/\s*(\d{1,6})\b")
_SAYI_RE = re.compile(r"\d[\d.,]*\d|\d")
_GUN_YAZI = {7: ["bir hafta", "1 hafta", "yedi gün"], 14: ["iki hafta", "2 hafta", "on dört gün", "ondört gün"],
             15: ["on beş gün", "onbeş gün"], 30: ["otuz gün", "bir ay", "1 ay"], 10: ["on gün"], 8: ["sekiz gün"],
             60: ["altmış gün", "iki ay", "2 ay"], 90: ["doksan gün", "üç ay", "3 ay"], 3: ["üç gün"], 5: ["beş gün"]}


class _Kaynak:
    """Kaynak metnin tarih/numara/sayı kümeleri (bir kez çıkarılır)."""

    def __init__(self, metin: str):
        # PDF'ten çıkan metinde kelimeler arası çoğu kez BÖLÜNMEZ boşluk (U+00A0) ya da satır sonu: "iki hafta"
        # bulunamıyor, doğru SÜRE satırı siliniyordu (2026-10-26, canlı ölçüm). Tüm boşluklar tek boşluğa.
        metin = re.sub(r"\s+", " ", metin or "")
        self.kucuk = kalite._kucuk(metin)
        self.tarihler = {(int(d), int(m), int(y)) for d, m, y in _TARIH_RE.findall(metin)}
        self.tarihler |= {(int(d), _AYLAR[a], int(y)) for d, a, y in _TARIH_YAZI_RE.findall(self.kucuk)}
        self.nolar = {(int(a), int(b)) for a, b in _NO_RE.findall(metin)}
        self.sayilar = {re.sub(r"\D", "", x).lstrip("0") for x in _SAYI_RE.findall(metin)}

    def ihlaller(self, satir: str) -> list[str]:
        """Satırda kaynakta geçmeyen tarih, yıl/numara ya da ≥3 haneli sayı."""
        kotu = []
        for d, m, y in _TARIH_RE.findall(satir):
            if (int(d), int(m), int(y)) not in self.tarihler:
                kotu.append(f"{d}.{m}.{y}")
        temiz = _TARIH_RE.sub(" ", satir)
        for a, b in _NO_RE.findall(temiz):
            if (int(a), int(b)) not in self.nolar:
                kotu.append(f"{a}/{b}")
        temiz = _NO_RE.sub(" ", temiz)
        for x in _SAYI_RE.findall(temiz):
            rakam = re.sub(r"\D", "", x).lstrip("0")
            if len(rakam) >= 3 and rakam not in self.sayilar:
                kotu.append(x)
        return kotu

    def gun_var(self, gun: int) -> bool:
        if re.search(rf"(?<!\d){gun}\s*(?:\(.*?\)\s*)?(?:gün|günlük|iş günü)", self.kucuk):
            return True
        return any(y in self.kucuk for y in _GUN_YAZI.get(gun, []))


_DOGRULANAMADI = "[belgede doğrulanamadı]"


def dogrula(ozet: str, kaynak_metin: str) -> str:
    """
    Özeti kaynağa karşı denetler (istem değil KOD — uydurma tarih Takvim'e son gün olarak yazılabiliyordu):
      - DOSYA/TARİH künye değerinde kaynakta olmayan tarih/numara → değer "[belgede doğrulanamadı]".
      - SÜRE: başlangıç tarihi kaynakta yoksa BOŞALTILIR; gün sayısı kaynakta yoksa satır tamamen ATILIR
        (yanlış son gün hesaplanmasın); dayanak numarası kaynakta yoksa "doğrulayın" notu.
      - Bölüm maddeleri: kaynakta olmayan tarih/numara/≥3 haneli sayı içeren madde ATILIR.
    """
    k = _Kaynak(kaynak_metin)
    cikti = []
    for ham in ozet.splitlines():
        s = ham.strip()
        m = re.match(r"^(DOSYA|TARİH)\s*:\s*(.*)$", s)
        if m:
            cikti.append(f"{m.group(1)}: {_DOGRULANAMADI}" if k.ihlaller(m.group(2)) else s)
            continue
        m = re.match(r"^SÜRE\s*:\s*(.*)$", s)
        if m:
            p = [x.strip() for x in m.group(1).split("|")] + ["", "", "", ""]
            ne, tarih, gun, dayanak = p[:4]
            g = int(re.sub(r"\D", "", gun) or 0)
            if not g or not k.gun_var(g):
                continue
            if tarih and k.ihlaller(tarih):
                tarih = ""
            if dayanak and not all(n in k.sayilar for n in re.findall(r"\d+", dayanak)):
                dayanak += " — belgede geçmiyor, doğrulayın"
            cikti.append(f"SÜRE: {ne} | {tarih} | {g} | {dayanak}")
            continue
        if re.match(r"^[-•*]\s+", s) and k.ihlaller(s.replace("**", "")):
            continue
        cikti.append(ham)
    return "\n".join(cikti).strip()


async def karar_ozeti(belge_id: str, kimlik: str) -> dict:
    """Kararın gerçek metnini alır ve özetler. Kaynağı da döndürür (UYAP linki)."""
    if not llm.hazir():
        raise OzetHatasi("Özet bu sunucuda kapalı.")
    if hazir := onbellekten("karar", belge_id):
        return hazir
    veri = await ictihat.karar(belge_id, kimlik)   # ictihat hatalarını çağıran ele alır
    ozet, tam = await _ozetle(veri.get("metin", ""), _SISTEM_KARAR, "karar")
    return _sakla("karar", belge_id, {"ozet": ozet, "kaynak": veri.get("kaynak")}, tam)


async def mevzuat_ozeti(mevzuat_id: str, kimlik: str) -> dict:
    if not llm.hazir():
        raise OzetHatasi("Özet bu sunucuda kapalı.")
    if hazir := onbellekten("mevzuat", mevzuat_id):
        return hazir
    veri = await mevzuat.icerik(mevzuat_id, kimlik)
    ozet, tam = await _ozetle(veri.get("metin", ""), _SISTEM_MEVZUAT, "mevzuat")
    return _sakla("mevzuat", mevzuat_id, {"ozet": ozet, "kaynak": veri.get("kaynak")}, tam)


async def belge_ozeti(metin: str) -> dict:
    """Yüklenen bir belgenin ÇIKARILMIŞ metnini özetler (metin çıkarma çağırana ait)."""
    if not llm.hazir():
        raise OzetHatasi("Özet bu sunucuda kapalı.")
    ozet, _tam = await _ozetle(metin, _SISTEM_BELGE, "belge")
    return {"ozet": ozet, "kaynak": None}


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "test":
        # Kod düzeyinde doğrulama: uydurma tarih/numara/gün düşer, gerçekleri kalır.
        kaynak = ("ANKARA 3. ASLİYE HUKUK MAHKEMESİ Esas No: 2025/1184. Bilirkişi raporu 18.09.2026 tarihinde "
                  "tebliğ edilmiştir. Taraflar HMK m. 281 uyarınca iki hafta içinde itiraz edebilir. Alacak 125.000,00 TL.")
        ozet = """TÜR: BİLİRKİŞİ RAPORU
DOSYA: 2025/1184
TARİH: 19.09.2026
SÜRE: Rapora itiraz | 18.09.2026 | 14 | HMK m. 281
### Önemli Tarih, Tutar ve Numaralar
- Alacak **125.000,00 TL**
- Faiz **3.450,00 TL** olarak hesaplanmıştır
- Rapor **18 Eylül 2026**'da tebliğ edildi"""
        d = dogrula(ozet, kaynak)
        assert "DOSYA: 2025/1184" in d and "TARİH: [belgede doğrulanamadı]" in d, d
        assert "SÜRE: Rapora itiraz | 18.09.2026 | 14 | HMK m. 281" in d, d
        assert "125.000,00" in d and "3.450,00" not in d, d
        d2 = dogrula("SÜRE: İtiraz | 01.01.2026 | 30 | HMK m. 999", kaynak)
        assert d2 == "", d2  # 30 gün kaynakta yok → satır tamamen düşer (yanlış son gün yok)
        assert dogrula("SÜRE: İstinaf |  | 14 | ", "tebliğinden\xa0itibaren\xa0iki\xa0hafta\niçinde istinaf").startswith("SÜRE: İstinaf"), \
            "bölünmez boşluk/satır sonu süreyi düşürmemeli"
        d3 = dogrula("SÜRE: İtiraz | 01.01.2026 | 14 | HMK m. 999", kaynak)
        assert d3 == "SÜRE: İtiraz |  | 14 | HMK m. 999 — belgede geçmiyor, doğrulayın", d3
        assert _Kaynak("on beş gün içinde").gun_var(15) and not _Kaynak("15 TL").gun_var(15)
        print("ozet.py doğrulama: tamam")
        # Uzun karar: baş + son (hüküm) birlikte.
        uzun = "BAŞ " + "x" * 60_000 + " HÜKÜM: davanın reddine"
        k_, kis = _kirp_karar(uzun)
        assert kis and k_.startswith("BAŞ") and k_.endswith("davanın reddine") and len(k_) < MAKS_METIN + 100
        assert len(_parcala("a\n" * 50_000)) == 4
        # Kesik/eksik özet önbelleğe girmez.
        _sakla("karar", "kesik", {"ozet": "y", "kaynak": None}, tam=False)
        assert onbellekten("karar", "kesik") is None
        # Önbellek: isabette LLM/UYAP'a hiç gidilmez; tavan aşılınca en eskisi düşer.
        _sakla("karar", "1", {"ozet": "x", "kaynak": None})
        llm.hazir = lambda: True  # anahtarsız ortamda da önbellek yolu sınansın
        assert asyncio.run(karar_ozeti("1", "anon")) == {"ozet": "x", "kaynak": None}
        assert onbellekten("mevzuat", "1") is None
        for i in range(_ONBELLEK_MAKS + 5):
            _sakla("mevzuat", str(i), {"ozet": str(i), "kaynak": None})
        assert len(_ONBELLEK) == _ONBELLEK_MAKS and onbellekten("karar", "1") is None
        print("ozet.py önbellek: tamam")
        raise SystemExit(0)

    if len(sys.argv) < 3 or sys.argv[1] not in ("karar", "mevzuat"):
        print("kullanım: python server/ozet.py karar|mevzuat <id>")
        raise SystemExit(1)
    tur, belge_id = sys.argv[1], sys.argv[2]

    async def _dene() -> None:
        try:
            sonuc = await (karar_ozeti if tur == "karar" else mevzuat_ozeti)(belge_id, "anon")
            print("KAYNAK:", sonuc["kaynak"])
            print("ÖZET:\n" + sonuc["ozet"])
        except (OzetHatasi, ictihat.IctihatHatasi, mevzuat.MevzuatHatasi) as e:
            print("BAŞARISIZ:", e)

    asyncio.run(_dene())
