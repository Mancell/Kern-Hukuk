"""
Ortak kalite çekirdeği — dilekçe motorunda kaliteyi sağlayan parçalar, diğer yapay zekâ hizmetleri de kullansın diye
burada (kullanıcı kararı 2026-10-23: "diğer hizmetler de bu kadar kaliteli olsun").

  Kaynaklar      ajan turunda araçların GERÇEKTEN döndürdüğü madde/karar/karar metni + belgeler: doğrulama kümesi.
  denetle        metindeki E./K. numaraları, "<kanun> m.<n>" atıfları, *italik* ve `>` alıntıları kümeye karşı denetler.
  temizle        ikinci denemeden sonra kalan ihlalli cümle/satırı ATAR (uydurma künye meslek riski).
  ana_ile        yalnız ana model (DeepSeek); geçici hatada bekleyip yeniden dener, 401/402/403'te açık hata.

İstem değil KOD: model ne derse desin, araçla doğrulanmamış numara metinde kalamaz.
"""
from __future__ import annotations

import asyncio
import json
import re

import llm


class KaliteHatasi(Exception):
    """Kullanıcıya gösterilebilir hata (ana model yok vb.)."""


_KISALTMA = {
    "TBK": "6098", "HMK": "6100", "TMK": "4721", "İİK": "2004", "TTK": "6102", "TCK": "5237",
    "CMK": "5271", "İYUK": "2577",
}

_TR_KUCUK = str.maketrans({"I": "ı", "İ": "i"})
_TR_BUYUK = str.maketrans({"i": "İ", "ı": "I"})


def _kucuk(s: str) -> str:
    return s.translate(_TR_KUCUK).lower()


def _norm(s: str) -> str:
    """Alıntı karşılaştırması için: Türkçe küçük harf, tire ile bölünmüş kelimeler birleşik, tırnak/boşluk farkı yok."""
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s or "")
    s = _kucuk(s)
    s = re.sub(r"[\"'“”‘’«»`]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,;:")


def _baslik_harf(ad: str) -> str:
    """'TÜRK BORÇLAR KANUNU' → 'Türk Borçlar Kanunu' (Türkçe güvenli, bağlaçlar küçük)."""
    kelimeler = []
    for i, k in enumerate((ad or "").split()):
        kk = _kucuk(k)
        if i and kk in ("ve", "ile", "ya", "veya", "da", "de"):
            kelimeler.append(kk)
        else:
            kelimeler.append(kk[:1].translate(_TR_BUYUK).upper() + kk[1:])
    return " ".join(kelimeler)


def _tek_satir(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def _madde_no(madde: str) -> str:
    return re.sub(r"\s+", "", str(madde or "")).lower()


# --------------------------------------------------------------------------
# Kaynak kümesi — ajan turunda araçların döndürdükleri (süzgecin tek doğruluk kaynağı)
# --------------------------------------------------------------------------
class Kaynaklar:
    def __init__(self, belgeler: list[dict], arac=None):
        """`arac`: araç çağrısını yürüten fonksiyon (asistan._arac_calistir) — döngüsel içe aktarım olmasın diye
        parametre."""
        self.belgeler = belgeler
        self._arac = arac
        self.mevzuat: dict[str, dict] = {}            # id → {ad, no}
        self.maddeler: dict[tuple[str, str], str] = {}  # (mevzuat_id, madde) → metin  (DOĞRULANDI)
        self.kararlar: dict[str, dict] = {}            # id → {daire, esasNo, kararNo, tarih}
        self.karar_metni: dict[str, str] = {}          # id → okunan metin
        self._belge_norm = [_norm(b["metin"]) for b in belgeler]

    async def calistir(self, cagri: dict, kimlik: str) -> str:
        sonuc = await self._arac(cagri, kimlik)
        fn = cagri.get("function") or {}
        try:
            arg = json.loads(fn.get("arguments") or "{}")
        except Exception:
            return sonuc
        self.kaydet(fn.get("name"), arg, sonuc)
        return sonuc

    def kaydet(self, ad: str | None, arg: dict, sonuc: str) -> None:
        try:
            if ad == "mevzuat_ara":
                for k in json.loads(sonuc).get("kayitlar") or []:
                    if k.get("id"):
                        self.mevzuat[str(k["id"])] = {"ad": k.get("ad") or "", "no": str(k.get("no") or ""),
                                                      "url": k.get("uyap") or ""}
            elif ad == "ictihat_ara":
                for k in json.loads(sonuc).get("kararlar") or []:
                    if k.get("id"):
                        self.kararlar[str(k["id"])] = {
                            "daire": k.get("daire") or "", "esasNo": k.get("esasNo") or "",
                            "kararNo": k.get("kararNo") or "", "tarih": k.get("tarih") or "",
                            "url": k.get("uyap") or "",
                        }
            elif ad == "karar_getir" and arg.get("id"):
                self.karar_metni[str(arg["id"])] = sonuc
            elif ad == "mevzuat_getir" and arg.get("madde") and "— DOĞRULANDI]" in sonuc.split("\n", 1)[0]:
                self.maddeler[(str(arg.get("id")), _madde_no(arg["madde"]))] = sonuc.split("\n", 1)[-1]
        except (ValueError, AttributeError, TypeError):
            pass

    # --- sorgular ---
    def kanun(self, mevzuat_id: str) -> dict | None:
        return self.mevzuat.get(str(mevzuat_id))

    def madde_dogru_mu(self, kanun_no: str, madde: str) -> bool:
        m = _madde_no(madde)
        return any(
            mm == m and (self.kanun(mid) or {}).get("no") == kanun_no for (mid, mm) in self.maddeler
        )

    def belgede_var(self, alinti: str) -> bool:
        """Alıntı belgede BİREBİR geçiyor mu. "…"/"..." ile kısaltılmışsa her parça aynı belgede SIRAYLA geçmeli
        (canlıda model maddeleri kısaltarak alıntılıyordu, hepsi "bulunamadı" sayılıyordu)."""
        parcalar = [_norm(p) for p in re.split(r"\s*(?:\.{3}|…)\s*", alinti or "")]
        parcalar = [p for p in parcalar if p]
        if not parcalar or sum(len(p) for p in parcalar) < 12:
            return False
        for b in self._belge_norm:
            yer = 0
            for p in parcalar:
                yer = b.find(p, yer)
                if yer < 0:
                    break
                yer += len(p)
            else:
                return True
        return False

    def belgedeki_atiflar(self) -> tuple[set[str], set[tuple[str, str]]]:
        """Belgelerde (kullanıcının belgesi/mesajı) ZATEN geçen E./K. numaraları ve "<kanun> m.<n>" atıfları:
        bunları tekrar etmek uydurma değildir (avukatın verdiği ya da karşı tarafın dayandığı kaynak)."""
        nolar = {f"{y}/{int(n)}" for b in self.belgeler for y, n in re.findall(r"(\d{4})\s*/\s*(\d+)", b.get("metin") or "")}
        maddeler = set()
        for ad, no in self.kanun_adlari().items():
            desen = re.compile(r"(?<![\wÇĞİÖŞÜçğıöşü])" + re.escape(ad)
                               + r"(?:['’]?n[ıiuü]n)?\s+m(?:adde(?:si)?)?\.?\s*(\d+[a-z]?)")
            for b in self.belgeler:
                maddeler |= {(no, _madde_no(m)) for m in desen.findall(b.get("metin") or "")}
        return nolar, maddeler

    def kararda_var(self, karar_id: str | None, alinti: str) -> bool:
        n = _norm(alinti)
        if len(n) < 12:
            return False
        metinler = [self.karar_metni.get(str(karar_id), "")] if karar_id else list(self.karar_metni.values())
        return any(n in _norm(m) for m in metinler if m)

    def kunye_metni(self, k: dict) -> str:
        """'Yargıtay 6. Hukuk Dairesi E.2021/4551 K.2022/3753, T.29.06.2022'"""
        daire = _tek_satir(k.get("daire"))
        if daire and not _kucuk(daire).startswith(("yargıtay", "danıştay", "anayasa", "bölge")):
            daire = "Yargıtay " + daire
        parcalar = [daire, f"E.{k.get('esasNo')}" if k.get("esasNo") else "", f"K.{k.get('kararNo')}" if k.get("kararNo") else ""]
        metin = " ".join(p for p in parcalar if p)
        return metin + (f", T.{k['tarih']}" if k.get("tarih") else "")

    def izin(self) -> dict:
        """Bu istekte DOĞRULANMIŞ her şey (`denetle`'nin izin kümesi): sohbet/analiz gibi başlık başına plan
        olmayan hizmetlerde metnin tamamı buna karşı denetlenir."""
        maddeler = [
            {"no": (self.kanun(mid) or {}).get("no", ""), "madde": mad, "kanun": _baslik_harf((self.kanun(mid) or {}).get("ad", ""))}
            for (mid, mad) in self.maddeler if self.kanun(mid)
        ]
        return {"maddeler": maddeler, "kararlar": list(self.kararlar.values())}

    def dayanak_bul(self, metin: str) -> dict | None:
        """Metinde DOĞRULANMIŞ bir madde ya da görülmüş bir karar geçiyorsa onun {"url"} bilgisini döndürür.
        Olumlu doğrulama: kanun adı/kısaltması + madde no birlikte geçmeli ya da kararın esas no'su geçmeli."""
        adlar = self.kanun_adlari()
        for (mid, mad) in self.maddeler:
            kanun = self.kanun(mid) or {}
            isimler = [a for a, no in adlar.items() if no == kanun.get("no")]
            if any(re.search(re.escape(a) + r"(?:['’]?n[ıiuü]n)?\s*m(?:adde(?:si)?)?\.?\s*" + re.escape(mad) + r"(?![\d/])",
                             metin, re.IGNORECASE) for a in isimler):
                return {"url": kanun.get("url", "")}
        for k in self.kararlar.values():
            e = (k.get("esasNo") or "").replace(" ", "")
            if e and e in metin.replace(" ", ""):
                return {"url": k.get("url", "")}
        return None

    def kanun_adlari(self) -> dict[str, str]:
        """Metinde aranacak kanun adı/kısaltması → numara."""
        adlar = dict(_KISALTMA)
        for k in self.mevzuat.values():
            if k.get("no") and k.get("ad"):
                adlar[_baslik_harf(k["ad"])] = k["no"]
        return adlar

# Kalite önce: ana model (DeepSeek) geçici olarak yanıt vermezse YEDEK MODELE GEÇİLMEZ, beklenir
# (kullanıcı kararı 2026-10-23). 401/402/403 (anahtar/bakiye) beklemekle düzelmez → açık hata.
ANA_BEKLEME_SN = 600.0
_ANA_ILK_ARA_SN = 10.0


async def _ana_ile(cagri, bildir=lambda _o: None, en_fazla: float | None = None):
    """`cagri()`yı yalnız ana modelle çalıştırır; geçici hatada artan aralıkla yeniden dener."""
    jeton = llm.yalniz_ana.set(True)
    try:
        bas, ara = asyncio.get_running_loop().time(), _ANA_ILK_ARA_SN
        while True:
            try:
                return await cagri()
            except (llm.LLMKalici, llm.LLMKapali):
                raise KaliteHatasi("Ana yapay zekâ modeline şu an erişilemiyor. Kaliteden ödün vermemek için yedek "
                                   "modele geçilmedi; lütfen daha sonra tekrar deneyin.")
            except llm.LLMHatasi:
                if asyncio.get_running_loop().time() - bas > (en_fazla or ANA_BEKLEME_SN):
                    raise KaliteHatasi("Ana yapay zekâ modeli uzun süredir yanıt vermiyor. Kaliteden ödün vermemek için "
                                       "yedek modele geçilmedi; birkaç dakika sonra tekrar deneyin.")
                bildir({"t": "durum", "metin": "Ana yapay zekâ modeli yoğun; kaliteden ödün vermemek için bekleniyor"})
                await asyncio.sleep(ara)
                ara = min(ara * 1.5, 45.0)
    finally:
        llm.yalniz_ana.reset(jeton)

_ESAS_RE = re.compile(r"\bE\.?\s*:?\s*(\d{4})\s*/\s*(\d+)")
_KARAR_RE = re.compile(r"\bK\.?\s*:?\s*(\d{4})\s*/\s*(\d+)")


def _sayi(s: str) -> str:
    m = re.search(r"(\d{4})\s*/\s*(\d+)", s or "")
    return f"{m.group(1)}/{int(m.group(2))}" if m else ""


def _denetle(metin: str, b: dict, k: Kaynaklar, italik: bool = True) -> list[str]:
    """Bölüm metnindeki atıf ve alıntıları denetler; ihlal açıklamaları döndürür. `italik`: tek yıldızlı *…*
    belgeden alıntı sayılır (dilekçe grameri); sohbet/analizde vurgudur, sayılmaz. **kalın** hiçbir zaman alıntı
    değildir (canlıda markdown kalın "uydurma alıntı" sanılıp cümleler siliniyordu)."""
    ihlal: list[str] = []
    belge_nolari, belge_maddeleri = k.belgedeki_atiflar()
    izinli_e = {_sayi(kr.get("esasNo", "")) for kr in b["kararlar"]} | belge_nolari
    izinli_k = {_sayi(kr.get("kararNo", "")) for kr in b["kararlar"]} | belge_nolari
    for m in _ESAS_RE.finditer(metin):
        if f"{m.group(1)}/{int(m.group(2))}" not in izinli_e:
            ihlal.append(f"izinsiz esas numarası E.{m.group(1)}/{m.group(2)}")
    for m in _KARAR_RE.finditer(metin):
        if f"{m.group(1)}/{int(m.group(2))}" not in izinli_k:
            ihlal.append(f"izinsiz karar numarası K.{m.group(1)}/{m.group(2)}")
    izinli_madde = {(m["no"], m["madde"]) for m in b["maddeler"]} | belge_maddeleri
    adlar = k.kanun_adlari()
    for ad, no in adlar.items():
        desen = re.compile(r"(?<![\wÇĞİÖŞÜçğıöşü])" + re.escape(ad)
                           + r"(?:['’]?n[ıiuü]n)?\s+m(?:adde(?:si)?)?\.?\s*(\d+[a-z]?(?:/\d+)?)")
        for m in desen.finditer(metin):
            if (no, _madde_no(m.group(1).split("/")[0])) not in izinli_madde:
                ihlal.append(f"izinsiz madde atfı {ad} m.{m.group(1)}")
    for m in (re.finditer(r"(?<!\*)\*(?!\*)([^*\n]{8,}?)(?<!\*)\*(?!\*)", metin) if italik else ()):
        if not (k.belgede_var(m.group(1)) or k.kararda_var(None, m.group(1))):
            ihlal.append(f"belgede birebir bulunmayan alıntı: {m.group(1)[:60]}…")
    for satir in metin.splitlines():
        if satir.strip().startswith(">"):
            alinti = satir.strip()[1:].strip()
            if not (k.kararda_var(None, alinti) or k.belgede_var(alinti)):
                ihlal.append(f"karar metninde bulunmayan alıntı: {alinti[:60]}…")
    return ihlal


def _temizle(metin: str, b: dict, k: Kaynaklar, italik: bool = True) -> str:
    """İkinci denemeden sonra kalan ihlalleri ATAR: ihlalli alıntı satırı ve ihlalli atıf içeren cümle."""
    cikti = []
    for satir in metin.splitlines():
        s = satir.strip()
        if not s:
            continue
        if s.startswith(">"):
            if _denetle(s, b, k, italik):
                continue
            cikti.append(s)
            continue
        cumleler = re.split(r"(?<=[.;])\s+(?=[A-ZÇĞİÖŞÜ0-9\"“*])", s)
        temiz = [c for c in cumleler if not _denetle(c, b, k, italik)]
        if temiz:
            cikti.append(" ".join(temiz))
    return "\n".join(cikti)

async def gorev_akisi(is_fn):
    """
    `is_fn(bildir)` işini ayrı görevde koşturup olaylarını akış olarak verir (NDJSON uçları için): iç içe bekleyen
    kod (UYAP sırası, ana model yoğunluğu, araç turları) da kullanıcıya durum bildirebilsin, istemci sessizlik
    sınırına takılmasın. İşin döndürdüğü sözlük en son olay olarak gelir; hata en sonda fırlar.
    """
    kuyruk: asyncio.Queue = asyncio.Queue()
    gorev = asyncio.create_task(is_fn(kuyruk.put_nowait))
    try:
        while True:
            al = asyncio.ensure_future(kuyruk.get())
            await asyncio.wait({al, gorev}, return_when=asyncio.FIRST_COMPLETED)
            if al.done():
                yield al.result()
                continue
            al.cancel()
            while not kuyruk.empty():
                yield kuyruk.get_nowait()
            sonuc = gorev.result()  # hata varsa burada fırlar
            if sonuc is not None:
                yield sonuc
            return
    finally:
        if not gorev.done():
            gorev.cancel()


# Herkese açık adlar (dilekce_motor alt çizgili adları içe aktarıyor; yeni çağıranlar bunları kullanır).
ana_ile = _ana_ile


def denetle(metin: str, b: dict, k: Kaynaklar) -> list[str]:
    """Sohbet/analiz denetimi: markdown vurgusu (*…*, **…**) alıntı sayılmaz."""
    return _denetle(metin, b, k, italik=False)


def temizle(metin: str, b: dict, k: Kaynaklar) -> str:
    return _temizle(metin, b, k, italik=False)
norm = _norm
tek_satir = _tek_satir
baslik_harf = _baslik_harf


if __name__ == "__main__":
    # Ağsız öz-test:  python server/kalite.py
    k = Kaynaklar([{"ad": "Sözleşme", "metin": "Madde 7: Kira bedeli her ayın beşinde ödenir."}])
    k.kaydet("mevzuat_ara", {}, json.dumps({"kayitlar": [{"id": "M1", "ad": "TÜRK BORÇLAR KANUNU", "no": "6098"}]}))
    k.kaydet("mevzuat_getir", {"id": "M1", "madde": "315"}, "[315. madde — DOĞRULANDI]\nKiracı kira bedelini…")
    k.kaydet("ictihat_ara", {}, json.dumps({"kararlar": [{"id": "K1", "daire": "3. Hukuk Dairesi",
                                                           "esasNo": "2020/111", "kararNo": "2021/222", "tarih": "01.02.2021"}]}))
    izin = k.izin()
    assert izin["maddeler"] == [{"no": "6098", "madde": "315", "kanun": "Türk Borçlar Kanunu"}]
    assert denetle("Türk Borçlar Kanunu m.315 ve Yargıtay 3. HD E.2020/111 K.2021/222 kararı.", izin, k) == []
    ihlal = _denetle("TBK m.316 ve E.2019/5 K.2020/6; sözleşme *her ayın onunda ödenir* der.", izin, k)
    assert len(ihlal) == 4, ihlal
    # Markdown kalın/vurgu alıntı DEĞİL (canlı hata): ne dilekçe ne sohbet denetimi bunu ihlal saymaz.
    assert _denetle("**Künyeyi doğrulayıp içeriğini getirmek:** mümkün", izin, k) == []
    assert denetle("Bu *önemli bir vurgu cümlesidir* ve alıntı değil.", izin, k) == []
    assert "316" not in temizle("İlk cümle doğru. TBK m.316 uydurma.", izin, k)
    assert k.dayanak_bul("TBK m. 315 (kiracının temerrüdü)") is not None
    assert k.dayanak_bul("Türk Borçlar Kanunu m.316") is None and k.dayanak_bul("Kat Mülkiyeti Kanunu m.18") is None
    assert k.dayanak_bul("Yargıtay 3. HD E.2020/111") is not None

    async def _akis_testi():
        async def is_(bildir):
            bildir({"t": "durum", "metin": "a"})
            await asyncio.sleep(0.01)
            return {"t": "sonuc", "x": 1}
        return [o async for o in gorev_akisi(is_)]

    assert asyncio.run(_akis_testi()) == [{"t": "durum", "metin": "a"}, {"t": "sonuc", "x": 1}]
    # Belgede/kullanıcı mesajında zaten geçen atıf tekrar edilebilir; kısaltılmış alıntı parça parça aranır.
    k2 = Kaynaklar([{"ad": "Mesaj", "metin": "Yargıtay 3. HD E.2019/4821 K.2020/1937 kararını ve TBK m. 344'ü sor."},
                    {"ad": "Söz", "metin": "Madde 2/5 – Arsa sahipleri, ruhsat ve tapu işlemleri için en geç yedi gün içinde vekâletname vermekle yükümlüdür."}])
    assert denetle("E.2019/4821 K.2020/1937 sayılı karar ve TBK m.344", k2.izin(), k2) == []
    assert denetle("E.2018/1 K.2019/2", k2.izin(), k2) != []
    assert k2.belgede_var("Arsa sahipleri, ruhsat ve tapu işlemleri için … vekâletname vermekle yükümlüdür.")
    assert not k2.belgede_var("vekâletname vermekle yükümlüdür … Arsa sahipleri, ruhsat")  # sıra önemli
    print("kalite: tamam")
