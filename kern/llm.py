"""
LLM sağlayıcı — TEK yer. `asistan.py` ve `sorgu_oneri.py` buradan geçer.

Neden tek dosya: `meta/llama-3.3-70b-instruct` 2026-08-26'da NVIDIA'da emekliye
ayrıldı ve model adı İKİ dosyada ayrı ayrı gömülü olduğu için ikisi de aynı anda
bozuldu (biri sessizce). Sağlayıcı/model ayarı artık burada; bir model daha
ölürse düzeltme kod değişikliği değil, Railway'de `LLM_MODEL` değişikliğidir.

Sağlayıcı OpenAI-uyumlu `/chat/completions` bekliyor. Google Gemini bunu
`https://generativelanguage.googleapis.com/v1beta/openai` altında sunuyor;
NVIDIA da OpenAI-uyumlu. İkisi de `Authorization: Bearer` kullandığı için
sağlayıcı değiştirmek = env değiştirmek:
    Gemini (varsayılan): GEMINI_API_KEY yeter.
    NVIDIA'ya dönmek:    LLM_TABAN=https://integrate.api.nvidia.com/v1
                         LLM_MODEL=<canlı model>  LLM_API_KEY=<nvidia anahtarı>

DENETLENEBİLİRLİK (CLAUDE.md değişmez #2): bu dosya `ictihat.py`'nin DIŞINDA.
İçtihat ARAMA yolunda model yok; `grep -i nvidia server/ictihat.py` boş kalır.

KAPASİTE TAVANI RAILWAY DEĞİL, ANAHTARIN KATMANI (2026-09-16 ölçüldü). "AI
özellikleri 4-10 kat yavaş" şikâyetinin kökü: GEMINI_API_KEY ücretsiz katmanda,
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` = **günde 20 istek** (tüm
kullanıcılar için toplam; bir analiz/dilekçe turu 2-6 istek yer). Kotaya
yaklaşınca Google isteği reddetmiyor, 40-80 sn BEKLETİYOR (aynı istek boşta
~2 sn); aşınca 429 + `retryDelay`. Tek bir çağrı Railway'den de bilgisayardan
da ~2 sn — ağ/proxy sorunu yok, eski "Railway 10x yavaş" notu yanlıştı.
Çözüm kod değil faturalandırma (Gemini ücretli katman) ya da sağlayıcı
(NVIDIA anahtarı Railway'de tanımlı). Kod tarafı yalnız zararı azaltır:
429/5xx'te `retryDelay` kısa ise bekleyip tekrar dener, uzunsa kullanıcıya
"N saniye" der (eskiden opak "HTTP 429" idi ve hiç tekrar denemiyordu).
"""
import asyncio
import contextvars
import json
import os
import re
import time

import httpx

import olcum
import token_kayit

# SAĞLAYICI ZİNCİRİ (kullanıcı kararı 2026-10-13: "DeepSeek Flash her yerde, hızlı olsun,
# 'LLM yanıt üretemedi' hatası görmeyeyim"). Her istek zincirin başından denenir; bir
# sağlayıcı 402 (bakiye) / 401-403 (anahtar) / 429 (kota) / 5xx / zaman aşımı / ağ hatası
# verirse BEKLEMEDEN sıradakine geçilir ve o sağlayıcı bir süre dinlendirilir (sonraki
# istekler onu sona koyar — 402'yi her istekte yeniden ödemesin). Dinlenen sağlayıcı da
# en sonda yine denenir: hepsi düşmeden kullanıcıya hata gösterilmez.
#   1) Ana: `DeepSeekAPI` varsa DeepSeek `deepseek-flash`, yoksa Gemini `gemini-3.6-flash`.
#      LLM_TABAN / LLM_MODEL / LLM_API_KEY yalnız BU halkayı değiştirir (ör. NVIDIA).
#   2) Gemini `LLM_YEDEK_MODEL` (varsayılan `gemini-3.5-flash-lite`): ücretsiz katmanda kota
#      MODEL BAŞINA ayrı, lite ~2 sn (2026-10-13 ölçüldü).
#   3) Gemini `gemini-3.6-flash` (ana DeepSeek iken son çare).
# KVKK: DeepSeek sunucuları Çin'de — belge metni de oraya gider (CLAUDE.md'deki dışa akış notu).
_GEMINI_TABAN = "https://generativelanguage.googleapis.com/v1beta/openai"
_DEEPSEEK_TABAN = "https://api.deepseek.com"
_DEEPSEEK = os.getenv("DeepSeekAPI", "").strip()
_GEMINI = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()

# Gemini 3.x flash bir "thinking" modeli: gizli düşünme çıktı bütçesini yiyor
# (ölçüldü: ~380 token @ low, ~530 @ medium) ve max_tokens düşünme+cevabı BİRLİKTE
# sınırlıyor. `reasoning_effort=low` bunu dizginliyor (`none` bu modelde 400 veriyor).
# DeepSeek'e bu alan GÖNDERİLMEZ. Ana halka için LLM_REASONING_EFFORT ile ezilir.
_DEEPSEEK_DUSUNSUN = os.getenv("LLM_DEEPSEEK_DUSUNME", "").strip() == "1"


def _halka(taban: str, anahtar: str, model: str, reasoning: str | None = None) -> dict:
    if reasoning is None:
        reasoning = "" if "deepseek" in taban else "low"
    return {"taban": taban, "anahtar": anahtar, "model": model, "reasoning": reasoning}


def _zincir_kur() -> list[dict]:
    taban = os.getenv("LLM_TABAN") or (_DEEPSEEK_TABAN if _DEEPSEEK else _GEMINI_TABAN)
    ds = "deepseek" in taban
    adaylar = [
        _halka(
            taban,
            (os.getenv("LLM_API_KEY") or (_DEEPSEEK if ds else _GEMINI)).strip(),
            os.getenv("LLM_MODEL") or ("deepseek-flash" if ds else "gemini-3.6-flash"),
            os.getenv("LLM_REASONING_EFFORT", "" if ds else "low").strip(),
        ),
        _halka(_GEMINI_TABAN, _GEMINI, os.getenv("LLM_YEDEK_MODEL", "").strip() or "gemini-3.5-flash-lite"),
        _halka(_GEMINI_TABAN, _GEMINI, "gemini-3.6-flash"),
    ]
    zincir: list[dict] = []
    for h in adaylar:
        if h["anahtar"] and not any((z["taban"], z["model"]) == (h["taban"], h["model"]) for z in zincir):
            zincir.append(h)
    return zincir


_ZINCIR = _zincir_kur()
_dinlenme: dict[int, float] = {}  # halka sırası → monotonic bitiş

# 429/5xx veren halka bu kadar saniye sona alınır: Gemini "high demand" 503'ü ~8 sn
# bekletip dönüyor (ölçüldü) — her istek bunu ödemesin.
ANA_DINLENME_SN = float(os.getenv("LLM_ANA_DINLENME_SN", "900"))
# 401/402/403: anahtar/bakiye kendiliğinden düzelmez; bakiye yüklenince en geç bu kadar sonra devreye girer.
KALICI_DINLENME_SN = float(os.getenv("LLM_KALICI_DINLENME_SN", "600"))
AG_DINLENME_SN = 60.0
# Tek LLM çağrısının (tüm zincir dahil) tavanı. Canlıda ölçüldü: karar metniyle cevap
# yazan agent turu 55 sn'yi aşabiliyor; telefon 180 sn bekliyor (src/device/asistan.ts).
ZAMAN_ASIMI_SN = float(os.getenv("LLM_ZAMAN_ASIMI_SN", "100"))
_TEKRAR_KODLARI = {429, 500, 502, 503, 504}
_GENEL_HATA = "Yapay zeka şu an yanıt veremiyor; birkaç saniye sonra tekrar deneyin."


class LLMHatasi(Exception):
    """LLM çağrısı başarısız. Çağıran kullanıcıya gösterilebilir bir mesaja çevirir."""


class LLMKapali(LLMHatasi):
    """Anahtar tanımsız — özellik bu sunucuda kapalı."""


class LLMKalici(LLMHatasi):
    """Yalnız-ana modda ana sağlayıcı 401/402/403 verdi (anahtar/bakiye): beklemekle düzelmez."""


# Kalite önce (kullanıcı kararı 2026-10-23: "DeepSeek her yerde, ücretini veriyoruz"): VARSAYILAN olarak YALNIZ
# zincirin ana halkası (DeepSeek Flash) denenir, yedek Gemini'ye GEÇİLMEZ. Geçici hatada çağıran bekleyip yeniden
# dener (kalite.ana_ile); 401/402/403 LLMKalici olur. Yedek zincir kodda durur: yalnız bilerek `False` verilen
# bağlamda devreye girer.
yalniz_ana: contextvars.ContextVar[bool] = contextvars.ContextVar("llm_yalniz_ana", default=True)

# Son çağrının modeli ve bitiş nedeni ("stop"/"length"/"tool_calls"): `tamamla`/`konus` metin/mesaj döndürüyor,
# kesik ve modeli çağıran buradan okur (önbelleğe yalnız tam + ana modelden gelen yanıt girsin diye).
son_yanit: contextvars.ContextVar[dict] = contextvars.ContextVar("llm_son_yanit", default={})


class LLMKota(LLMHatasi):
    """Sağlayıcı kotası dolu / yoğun; `saniye` sonra tekrar denenebilir.
    Mesaj "N saniye" kalıbını taşır — GeriSayim bunu okuyor (bedesten.py ile aynı)."""

    def __init__(self, saniye: int):
        if saniye <= 300:
            mesaj = f"Yapay zeka servisi şu an yoğun; {saniye} saniye sonra tekrar deneyin."
        else:
            # Günlük kota: "45102 saniye" geri sayımı anlamsız (GeriSayim yalnız "N saniye"yi sayar).
            sure = f"yaklaşık {round(saniye / 3600)} saat" if saniye >= 5400 else f"yaklaşık {round(saniye / 60)} dakika"
            mesaj = f"Yapay zeka günlük kullanım sınırına ulaştı; {sure} sonra yeniden kullanılabilir."
        super().__init__(mesaj)
        self.saniye = saniye


def _bekleme_sn(yanit: httpx.Response) -> float | None:
    """Retry-After başlığı, yoksa Gemini'nin gövdeye yazdığı `retryDelay: "37s"` /
    'Please retry in 37.3s'. Hiçbiri yoksa None."""
    ra = (yanit.headers.get("retry-after") or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", ra):
        return float(ra)
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', yanit.text) or re.search(
        r"retry in (\d+(?:\.\d+)?)s", yanit.text
    )
    return float(m.group(1)) if m else None


def hazir() -> bool:
    return bool(_ZINCIR)


def _sira() -> list[int]:
    """Dinlenmeyen halkalar zincir sırasıyla, dinlenenler en sonda (en erken uyanan önce)."""
    simdi = time.monotonic()
    tum = range(len(_ZINCIR))
    hazirlar = [i for i in tum if _dinlenme.get(i, 0.0) <= simdi]
    return hazirlar + sorted((i for i in tum if i not in hazirlar), key=lambda i: _dinlenme[i])


def _dinlendir(i: int, sn: float) -> None:
    _dinlenme[i] = time.monotonic() + sn


def _basarisiz(i: int, yanit: httpx.Response | None) -> float | None:
    """Halkayı koda göre dinlendirir. Kota (429) ise önerilen bekleme süresini döndürür
    (hepsi kotadaysa kullanıcıya "N saniye" denir), değilse None."""
    if yanit is None:  # zaman aşımı / ağ / çözümlenemeyen gövde
        _dinlendir(i, AG_DINLENME_SN)
        return None
    kod, bekle = yanit.status_code, _bekleme_sn(yanit)
    if kod in (401, 402, 403):
        _dinlendir(i, KALICI_DINLENME_SN)
    elif kod in _TEKRAR_KODLARI:
        _dinlendir(i, max(bekle or 0.0, ANA_DINLENME_SN))
    # 400 vb. isteğe özgü: dinlendirme yok, yalnız sıradakine geçilir.
    return (bekle or 30.0) if kod == 429 else None


def _son_hata(sebepler: list[float | None]) -> LLMHatasi:
    if sebepler and all(s is not None for s in sebepler):
        return LLMKota(max(1, int(round(min(sebepler)))))
    return LLMHatasi(_GENEL_HATA)


# Google'ın belgelediği yer tutucu imza: araç çağrısı başka bir sağlayıcıdan (DeepSeek) geldiyse
# gerçek imza yoktur; Gemini'ye geçilince ikinci tur 400 almasın.
_SAHTE_IMZA = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _imzala(mesajlar: list) -> list:
    """Gemini'ye giden geçmişte imzasız araç çağrılarına yer tutucu imza koyar (kopya üstünde)."""
    cikti = []
    for m in mesajlar:
        if isinstance(m, dict) and m.get("tool_calls") and any("extra_content" not in c for c in m["tool_calls"]):
            m = dict(m, tool_calls=[c if "extra_content" in c else dict(c, extra_content=_SAHTE_IMZA) for c in m["tool_calls"]])
        cikti.append(m)
    return cikti


def _istek(h: dict, govde: dict) -> dict:
    g = dict(govde, model=h["model"])
    if "messages" in g:
        if "generativelanguage" in h["taban"]:
            g["messages"] = _imzala(g["messages"])
        else:  # Gemini'ye özel alan başka sağlayıcıya gitmesin (tanımayan 400 verebilir)
            g["messages"] = [
                dict(m, tool_calls=[{k: v for k, v in c.items() if k != "extra_content"} for c in m["tool_calls"]])
                if isinstance(m, dict) and m.get("tool_calls") else m
                for m in g["messages"]
            ]
    if h["reasoning"]:
        g["reasoning_effort"] = h["reasoning"]
    dusun = bool(g.pop("_dusun", False))
    if "deepseek" in h["taban"] and not (_DEEPSEEK_DUSUNSUN or dusun):
        # deepseek-flash varsayılan olarak ÖNCE düşünüyor (2026-10-19 ölçüldü): 800 token'lık bütçenin
        # tamamı düşünmeye gitti, görünür cevap 0; kapalıyken ilk kelime 0,9 sn, tam cevap 1,6 sn.
        # Asistan "geç cevap veriyor" şikâyetinin sebebi buydu. Geri açmak: LLM_DEEPSEEK_DUSUNME=1.
        g["thinking"] = {"type": "disabled"}
    return g


def _denemeler(timeout: float):
    """(halka sırası, kalan süre) üretir: önce tüm zincir, hiçbiri kota değilse 1 sn sonra
    bir tur daha. Çağıran başarıda döngüden çıkar; bitince `_son_hata` atar."""
    son = time.monotonic() + timeout
    if yalniz_ana.get():
        if _ZINCIR:
            yield 0, timeout
        return
    for tur in range(2):
        for i in _sira():
            kalan = son - time.monotonic()
            if kalan < 3:
                return
            yield i, kalan


async def tamamla(
    mesajlar: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    timeout: float = ZAMAN_ASIMI_SN,
) -> str:
    """
    OpenAI-uyumlu /chat/completions çağrısı, içerik metnini döndürür.

    `mesajlar` OpenAI biçiminde ({"role","content"}). Hata durumunda LLMHatasi;
    çağıran (asistan/sorgu_oneri) bunu kendi kullanıcı mesajına çeviriyor. Hata
    metnine sağlayıcının ham gövdesini KOYMUYORUZ (httpx bazen URL/anahtar sızdırır).
    """
    msg = await _cagir(
        {"messages": mesajlar, "temperature": temperature, "max_tokens": max_tokens},
        timeout,
    )
    icerik = (msg.get("content") or "").strip()
    if not icerik:
        raise LLMHatasi(_GENEL_HATA)
    return icerik


async def yaz(
    mesajlar: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    timeout: float = ZAMAN_ASIMI_SN,
    dusun: bool = False,
) -> tuple[str, bool]:
    """
    Uzun metin üretimi (dilekçe bölümleri): `(metin, kesik)` döndürür. `kesik` = sağlayıcı çıktıyı
    `max_tokens`'ta kesti (`finish_reason == "length"`); `tamamla` bunu göremiyor, yarım metni tam sanıyordu.
    `dusun`: DeepSeek düşünme modunu bu çağrı için açar (varsayılan kapalı; bkz. `_istek`).
    """
    msg, bitis = await _cagir(
        {"messages": mesajlar, "temperature": temperature, "max_tokens": max_tokens, "_dusun": dusun},
        timeout,
        bitis_ile=True,
    )
    icerik = (msg.get("content") or "").strip()
    if not icerik:
        raise LLMHatasi(_GENEL_HATA)
    return icerik, bitis == "length"


async def konus(
    mesajlar: list[dict],
    *,
    araclar: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: float = ZAMAN_ASIMI_SN,
) -> dict:
    """
    Araç (function-calling) destekli tek tur. Ham 'assistant' mesajını döndürür
    (content + varsa tool_calls); çağıran araç döngüsünü kendi yürütür.
    """
    return await _cagir(
        {"messages": mesajlar, "temperature": temperature, "max_tokens": max_tokens, "tools": araclar},
        timeout,
    )


async def _cagir(govde: dict, timeout: float, bitis_ile: bool = False):
    """Ortak POST, zincir boyunca. `choices[0].message` sözlüğünü (`bitis_ile`: + finish_reason) döndürür."""
    if not _ZINCIR:
        raise LLMKapali("LLM anahtarı tanımsız.")
    sebepler: list[float | None] = []
    for i, kalan in _denemeler(timeout):
        if len(sebepler) == len(_ZINCIR) and all(s is not None for s in sebepler):
            break  # hepsi kotada: ikinci tur kotayı daha da harcar
        h = _ZINCIR[i]
        yanit = None
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(base_url=h["taban"], timeout=httpx.Timeout(kalan, connect=min(5.0, kalan))) as c:
                yanit = await c.post(
                    "/chat/completions", headers={"Authorization": f"Bearer {h['anahtar']}"}, json=_istek(h, govde)
                )
            olcum.kaydet(f"llm:{h['model']}", time.monotonic() - t0)
            if yanit.status_code == 200:
                veri = yanit.json()
                msg = veri["choices"][0]["message"]
                _dinlenme.pop(i, None)
                token_kayit.kaydet(h["model"], veri.get("usage"))
                bitis = veri["choices"][0].get("finish_reason")
                son_yanit.set({"model": h["model"], "bitis": bitis, "ana": i == 0})
                return (msg, bitis) if bitis_ile else msg
        except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError):
            yanit = None
        sebepler.append(_basarisiz(i, yanit))
        if yalniz_ana.get() and yanit is not None and yanit.status_code in (401, 402, 403):
            raise LLMKalici(_GENEL_HATA)
        if len(sebepler) == len(_ZINCIR):
            await asyncio.sleep(1.0)
    raise _son_hata(sebepler)


async def konus_akis(
    mesajlar: list[dict],
    *,
    araclar: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: float = ZAMAN_ASIMI_SN,
):
    """
    `konus`un AKIŞLI hâli (SSE). Async generator: her içerik parçası için ("metin", str),
    bitince tek ("mesaj", {content, tool_calls?}) — `konus`un döndürdüğüyle aynı biçim.

    Akış BAŞLAMADAN önceki hatalar (402/429/5xx/ağ) `_cagir` ile aynı zinciri dolaşır;
    ilk parça geldikten sonra kopma LLMHatasi'dır (yarım yanıt sessizce tamam sayılmaz,
    başka sağlayıcıyla baştan yazılamaz — kullanıcı ilk yarıyı çoktan gördü).
    """
    if not _ZINCIR:
        raise LLMKapali("LLM anahtarı tanımsız.")
    govde = {"messages": mesajlar, "temperature": temperature, "max_tokens": max_tokens,
             "tools": araclar, "stream": True, "stream_options": {"include_usage": True}}
    icerik: list[str] = []
    cagrilar: dict[int, dict] = {}
    sebepler: list[float | None] = []
    tamam = False
    kullanim: dict | None = None
    bitis: str | None = None
    for i, kalan in _denemeler(timeout):
        if len(sebepler) == len(_ZINCIR) and all(s is not None for s in sebepler):
            break
        h = _ZINCIR[i]
        yanit = None
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(base_url=h["taban"], timeout=httpx.Timeout(kalan, connect=min(5.0, kalan))) as c:
                async with c.stream(
                    "POST", "/chat/completions", headers={"Authorization": f"Bearer {h['anahtar']}"},
                    json=_istek(h, govde),
                ) as yanit:
                    if yanit.status_code == 200:
                        async for satir in yanit.aiter_lines():
                            if not satir.startswith("data:"):
                                continue
                            veri = satir[5:].strip()
                            if veri == "[DONE]":
                                break
                            try:
                                parca = json.loads(veri)
                            except Exception:
                                continue
                            if parca.get("usage"):  # son parça (stream_options.include_usage)
                                kullanim = parca["usage"]
                            secimler = parca.get("choices") or []
                            if not secimler:
                                continue
                            if secimler[0].get("finish_reason"):
                                bitis = secimler[0]["finish_reason"]
                            delta = secimler[0].get("delta") or {}
                            if delta.get("content"):
                                icerik.append(delta["content"])
                                yield ("metin", delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                k = cagrilar.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                                              "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    k["id"] = tc["id"]
                                if tc.get("extra_content"):
                                    # Gemini 3'ün "düşünce imzası": araç sonucunu geri yollarken
                                    # yoksa ikinci tur 400 ("missing a thought_signature") — ölçüldü.
                                    k["extra_content"] = tc["extra_content"]
                                f = tc.get("function") or {}
                                if f.get("name"):
                                    k["function"]["name"] += f["name"]
                                if f.get("arguments"):
                                    k["function"]["arguments"] += f["arguments"]
                        tamam = True
                        token_kayit.kaydet(h["model"], kullanim)
                        olcum.kaydet(f"llm:{h['model']}", time.monotonic() - t0)
                        son_yanit.set({"model": h["model"], "bitis": bitis, "ana": i == 0})
                    else:
                        await yanit.aread()
        except httpx.RequestError:
            if icerik or cagrilar:
                raise LLMHatasi("Yapay zeka yanıtı yarıda kesildi; tekrar deneyin.")
            yanit = None
        if tamam:
            _dinlenme.pop(i, None)
            break
        sebepler.append(_basarisiz(i, yanit))
        if yalniz_ana.get() and yanit is not None and yanit.status_code in (401, 402, 403):
            raise LLMKalici(_GENEL_HATA)
        if len(sebepler) == len(_ZINCIR):
            await asyncio.sleep(1.0)
    if not tamam:
        raise _son_hata(sebepler)
    msg: dict = {"role": "assistant", "content": "".join(icerik) or None}
    if cagrilar:
        msg["tool_calls"] = [cagrilar[i] for i in sorted(cagrilar)]
        for i, c in enumerate(msg["tool_calls"]):
            c["id"] = c["id"] or f"call_{i}"
    yield ("mesaj", msg)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Ağsız birim: bekleme süresi ayrıştırma (Gemini'nin gerçek 429 gövdesi biçiminde).
    r = httpx.Response(429, text='[{"error": {"code": 429, "message": "... Please retry in 37.37975012s.",'
                                 ' "details": [{"@type": "x/RetryInfo", "retryDelay": "37s"}]}}]')
    assert _bekleme_sn(r) == 37.0, _bekleme_sn(r)
    assert _bekleme_sn(httpx.Response(429, text='{"error":"Please retry in 5.5s."}')) == 5.5
    assert _bekleme_sn(httpx.Response(503, headers={"retry-after": "12"}, text="")) == 12.0
    assert _bekleme_sn(httpx.Response(503, text="Service Unavailable")) is None
    assert "37 saniye" in str(LLMKota(37))
    # İmza: Gemini'ye imzasız araç çağrısı yer tutucuyla, gerçeği korunarak; başkasına alan gitmez.
    _gecmis = [{"role": "assistant", "tool_calls": [{"id": "a", "function": {}}, {"id": "b", "extra_content": {"x": 1}}]}]
    _gm = _istek(_halka(_GEMINI_TABAN, "k", "m"), {"messages": _gecmis})["messages"][0]["tool_calls"]
    assert _gm[0]["extra_content"] == _SAHTE_IMZA and _gm[1]["extra_content"] == {"x": 1}
    _dm = _istek(_halka(_DEEPSEEK_TABAN, "k", "m"), {"messages": _gecmis})["messages"][0]["tool_calls"]
    assert all("extra_content" not in c for c in _dm) and "extra_content" in _gecmis[0]["tool_calls"][1]

    # Zincir: sahte uçlarla (MockTransport) DeepSeek 402 → Gemini lite 503 → Gemini 200.
    _gercek_zincir, _gercek_istemci = list(_ZINCIR), httpx.AsyncClient
    _ZINCIR[:] = [_halka("https://deepseek.test", "k", "deepseek-flash"), _halka("https://g.test", "k", "lite"),
                  _halka("https://g.test", "k", "flash")]
    kodlar: dict[str, int] = {}
    gorulen: list[str] = []

    def _uc(istek: httpx.Request) -> httpx.Response:
        g = json.loads(istek.content)
        gorulen.append(g["model"])
        assert ("reasoning_effort" in g) == (g["model"] != "deepseek-flash"), g
        dusunen = g.get("max_tokens") == 7  # birimde `yaz(..., dusun=True)` işareti
        assert "_dusun" not in g, "iç alan sağlayıcıya gitmemeli"
        assert (g.get("thinking") == {"type": "disabled"}) == (g["model"] == "deepseek-flash" and not dusunen), g
        kod = kodlar[g["model"]]
        if g.get("max_tokens") in (6, 7):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "yarım"},
                                                          "finish_reason": "length"}]})
        if kod != 200:
            return httpx.Response(kod, text="")
        if g.get("stream"):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"mer"}}]}\n\n'
                                            'data: {"choices":[{"delta":{"content":"haba"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": g["model"]}}]})

    httpx.AsyncClient = lambda **kw: _gercek_istemci(transport=httpx.MockTransport(_uc), **kw)

    async def _akis() -> list:
        return [p async for p in konus_akis([], araclar=[], max_tokens=10, temperature=0)]

    async def _birim() -> None:
        # Zincir (yedeğe geçiş) davranışı yalnız-ana KAPALIYKEN sınanır; varsayılan artık yalnız-ana.
        yalniz_ana.set(False)
        kodlar.update({"deepseek-flash": 402, "lite": 503, "flash": 200})
        assert await tamamla([], max_tokens=10, temperature=0) == "flash"
        assert gorulen == ["deepseek-flash", "lite", "flash"], gorulen
        # Düşen ikisi dinleniyor: sonraki istek doğrudan çalışanla başlar.
        gorulen.clear()
        assert await tamamla([], max_tokens=10, temperature=0) == "flash" and gorulen == ["flash"], gorulen
        # Akış da aynı zinciri izler.
        gorulen.clear()
        _dinlenme.clear()
        parcalar = await _akis()
        assert parcalar[-1] == ("mesaj", {"role": "assistant", "content": "merhaba"}), parcalar
        assert gorulen == ["deepseek-flash", "lite", "flash"], gorulen
        # Hepsi kotada → beklemeden LLMKota ("N saniye"), ikinci tur yok.
        _dinlenme.clear()
        gorulen.clear()
        kodlar.update({"deepseek-flash": 429, "lite": 429, "flash": 429})
        try:
            await tamamla([], max_tokens=10, temperature=0)
            raise AssertionError("hata beklenirdi")
        except LLMKota:
            pass
        assert len(gorulen) == 3, gorulen
        # Karışık kalıcı hata → genel, dostça mesaj (asla "HTTP 402").
        _dinlenme.clear()
        kodlar.update({"deepseek-flash": 402, "lite": 500, "flash": 400})
        try:
            await tamamla([], max_tokens=10, temperature=0)
            raise AssertionError("hata beklenirdi")
        except LLMKota:
            raise AssertionError("kota değil")
        except LLMHatasi as e:
            assert str(e) == _GENEL_HATA
        # DeepSeek geri gelince (bakiye yüklendi) dinlenme bitince yine ilk o denenir.
        _dinlenme.clear()
        gorulen.clear()
        kodlar.update({"deepseek-flash": 200})
        assert await tamamla([], max_tokens=10, temperature=0) == "deepseek-flash" and gorulen == ["deepseek-flash"]
        # Yalnız-ana: yedeğe GEÇİLMEZ; geçici hata LLMHatasi, 402 LLMKalici.
        _dinlenme.clear()
        gorulen.clear()
        kodlar.update({"deepseek-flash": 503, "lite": 200, "flash": 200})
        _j = yalniz_ana.set(True)
        try:
            await tamamla([], max_tokens=10, temperature=0)
            raise AssertionError("yedeğe geçilmemeliydi")
        except LLMKalici:
            raise AssertionError("503 kalıcı sayılmamalı")
        except LLMHatasi:
            pass
        assert gorulen == ["deepseek-flash"], gorulen
        kodlar.update({"deepseek-flash": 402})
        try:
            await tamamla([], max_tokens=10, temperature=0)
            raise AssertionError("hata beklenirdi")
        except LLMKalici:
            pass
        kodlar.update({"deepseek-flash": 200})
        assert await tamamla([], max_tokens=10, temperature=0) == "deepseek-flash"
        yalniz_ana.reset(_j)
        _dinlenme.clear()
        assert son_yanit.get()["model"] == "deepseek-flash"
        # Kesik algılama + düşünme bayrağı (yalnız bu çağrı için).
        assert await yaz([], max_tokens=6, temperature=0) == ("yarım", True)
        assert await yaz([], max_tokens=7, temperature=0, dusun=True) == ("yarım", True)
        assert await yaz([], max_tokens=10, temperature=0) == ("deepseek-flash", False)

    asyncio.run(_birim())
    httpx.AsyncClient = _gercek_istemci
    _ZINCIR[:] = _gercek_zincir
    _dinlenme.clear()
    print("birim: tamam")

    print("ZİNCİR:", " → ".join(f"{h['taban'].split('//')[1].split('/')[0]}:{h['model']}" for h in _ZINCIR) or "YOK")
    if not _ZINCIR:
        print("Anahtar yok — canlı çağrı atlandı.")
        raise SystemExit(0)

    async def _dene() -> None:
        soru = " ".join(sys.argv[1:]) or "Tek kelimeyle: merhaba"
        t = time.monotonic()
        try:
            yanit = await tamamla([{"role": "user", "content": soru}], max_tokens=400, temperature=0.2)
            print(f"YANIT ({time.monotonic() - t:.1f} sn):", yanit)
        except LLMHatasi as e:
            print("BAŞARISIZ:", e)
        print("dinlenen:", {_ZINCIR[i]["model"]: round(b - time.monotonic()) for i, b in _dinlenme.items()})

    asyncio.run(_dene())
