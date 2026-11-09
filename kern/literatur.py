"""
Literatür arama — akademik makale/kitap (OpenAlex).

`ictihat.py` / `mevzuat.py`'nin akademik kardeşi ama BAŞKA bir arka uç: UYAP
Bedesten DEĞİL, OpenAlex (api.openalex.org) — ücretsiz, anahtarsız, açık akademik
indeks. Bu yüzden bedesten'in paylaşılan çıkış-IP kovasını KULLANMAZ; kendi
httpx istemcisi var. OpenAlex "kibar havuz" için istekte `mailto=` bekliyor
(OPENALEX_MAILTO env'i ile ayarlanır).

Kaynak: saidsurucu/literature-review-skills (MIT) — o skill Claude'un Chrome'una
adaptör enjekte ediyor; buradaki iş onun `scripts/adapters/openalex.js` arama
yolunun Python'a sadık taşınması (ters-çevrik özet yeniden kurma dâhil). Tarayıcı
gerektiren yayıncı kaynakları (Scholar/Scopus/WoS/…) sunucudan çalışmaz, kapsam
dışı. Sadece ARAMA: tam metin/kaynakça (PDF/DOM) mobil sunucuya uygun değil.

Gizlilik: buraya belge girmez, yalnızca avukatın yazdığı arama metni OpenAlex'e
gider (üçüncü taraf, ABD). Loglanmaz; sonuç künyesi UYAP gibi kaynağından gelir.

Tek başına çalışır:
    python server/literatur.py "kira sözleşmesi"
"""
import os
import re

import httpx

BASE = "https://api.openalex.org"
SAYFA_BOYU = 25
# Kibar havuz için iletişim; gerçek bir adres koymak OpenAlex'in önerisi.
_MAILTO = os.getenv("OPENALEX_MAILTO", "mancel-app@example.org").strip()
# Akademik sorgu geniş karakter kümesi içerebilir; kontrol karakterlerini ele,
# uzunluğu sınırla. (mevzuat'ın dar beyaz listesi burada uygun değil.)
_SORGU_MAKS = 200
_KOTU = re.compile(r"[\x00-\x1f\x7f]")
_OZET_MAKS = 5_000  # tek makale özet tavanı (bağlam/boyut)

_istemci: httpx.AsyncClient | None = None


class LiteraturHatasi(Exception):
    """Arama başarısız. Çağıran kullanıcıya gösterilebilir mesaja çevirir."""


def _get() -> httpx.AsyncClient:
    global _istemci
    if _istemci is None:
        _istemci = httpx.AsyncClient(
            base_url=BASE,
            timeout=20.0,
            headers={"User-Agent": f"mancel-literatur (+{_MAILTO})"},
        )
    return _istemci


async def kapat() -> None:
    global _istemci
    if _istemci is not None:
        await _istemci.aclose()
        _istemci = None


def _duz_doi(doi: str | None) -> str | None:
    """DOI'yi kanonik hâle getir: şema/host at, küçük harf."""
    if not doi:
        return None
    d = str(doi).strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:", "", d)
    return d or None


def _ozet_cevir(idx: dict | None) -> str | None:
    """
    OpenAlex özeti 'ters-çevrik indeks' (kelime → konumlar) olarak veriyor;
    kelimeleri konumlarına yerleştirip düz metne çeviriyoruz. (openalex.js
    `invAbstract`'ın Python karşılığı.)
    """
    if not idx:
        return None
    konumlar: list[tuple[int, str]] = []
    for kelime, yerler in idx.items():
        for p in yerler or []:
            if isinstance(p, int):
                konumlar.append((p, kelime))
    if not konumlar:
        return None
    konumlar.sort(key=lambda x: x[0])
    metin = " ".join(k for _, k in konumlar).strip()
    metin = re.sub(r"\s+", " ", metin)
    if len(metin) > _OZET_MAKS:
        metin = metin[:_OZET_MAKS].rstrip() + "…"
    return metin or None


def _makale(w: dict) -> dict:
    """Bir OpenAlex 'work' → normalize künye (openalex.js `mapWork`)."""
    loc = w.get("primary_location") or {}
    kaynak = loc.get("source") or {}
    en_iyi = w.get("best_oa_location") or {}
    yazarlar = [
        (a.get("author") or {}).get("display_name")
        for a in (w.get("authorships") or [])
    ]
    doi = _duz_doi(w.get("doi"))
    return {
        "id": str(w.get("id") or "").replace("https://openalex.org/", ""),
        "baslik": (w.get("display_name") or "").strip() or "(başlıksız)",
        "yazarlar": [y for y in yazarlar if y],
        "yil": w.get("publication_year"),
        "yer": kaynak.get("display_name"),
        "doi": doi,
        "tur": w.get("type"),
        "atifSayisi": w.get("cited_by_count") if isinstance(w.get("cited_by_count"), int) else None,
        "ozet": _ozet_cevir(w.get("abstract_inverted_index")),
        "pdf": en_iyi.get("pdf_url"),
        # Avukat kaynağı doğrulayabilmeli: DOI varsa doi.org, yoksa OpenAlex sayfası.
        "url": (f"https://doi.org/{doi}" if doi else w.get("id")) or None,
    }


async def ara(sorgu: str = "", sayfa: int = 1, kimlik: str = "anon") -> dict:
    """
    OpenAlex serbest-metin araması.

    `kimlik` imza simetrisi için var (OpenAlex per-kullanıcı kovası yok, kullanılmıyor).
    """
    sorgu = (sorgu or "").strip()
    if not sorgu:
        raise LiteraturHatasi("Arama metni boş olamaz.")
    if len(sorgu) > _SORGU_MAKS or _KOTU.search(sorgu):
        raise LiteraturHatasi("Geçersiz arama metni.")
    sayfa = max(1, min(int(sayfa), 100))

    try:
        yanit = await _get().get(
            "/works",
            params={
                "search": sorgu,
                "per-page": SAYFA_BOYU,
                "page": sayfa,
                "mailto": _MAILTO,
            },
        )
    except httpx.RequestError:
        raise LiteraturHatasi("Literatür servisine ulaşılamadı.")
    if yanit.status_code == 429:
        raise LiteraturHatasi("Literatür servisi yoğun; biraz sonra tekrar deneyin.")
    if yanit.status_code != 200:
        raise LiteraturHatasi(f"Literatür araması başarısız (HTTP {yanit.status_code}).")
    try:
        veri = yanit.json()
    except Exception:
        raise LiteraturHatasi("Literatür yanıtı çözümlenemedi.")

    meta = veri.get("meta") or {}
    kayitlar = [_makale(w) for w in (veri.get("results") or []) if w.get("id")]
    return {
        "toplam": meta.get("count") or 0,
        "sayfa": sayfa,
        "kaynak": "OpenAlex",
        "kayitlar": kayitlar,
    }


# --------------------------------------------------------------------------
# Duman testi:  python server/literatur.py "kira sözleşmesi"
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    # Ters-çevrik özet + DOI normalize: ağ gerekmez, asıl kırılgan yer burası.
    assert _ozet_cevir({"Kira": [0], "sözleşmesi": [1], "önemlidir": [2]}) == "Kira sözleşmesi önemlidir"
    assert _ozet_cevir(None) is None
    assert _ozet_cevir({}) is None
    assert _duz_doi("https://doi.org/10.1/AB") == "10.1/ab"
    assert _duz_doi("doi:10.2/x") == "10.2/x"
    assert _duz_doi(None) is None
    m = _makale({
        "id": "https://openalex.org/W123",
        "display_name": "Test",
        "authorships": [{"author": {"display_name": "A. Yılmaz"}}],
        "publication_year": 2020,
        "doi": "https://doi.org/10.1/x",
        "cited_by_count": 5,
        "abstract_inverted_index": {"hello": [0], "world": [1]},
    })
    assert m["id"] == "W123" and m["yazarlar"] == ["A. Yılmaz"] and m["doi"] == "10.1/x"
    assert m["ozet"] == "hello world" and m["url"] == "https://doi.org/10.1/x"
    print("birim: tamam")

    async def _dene() -> None:
        for kotu in ("", "x" * 300, "kira\x00"):
            try:
                await ara(kotu)
                raise AssertionError(f"geçersiz sorgu geçti: {kotu[:8]!r}")
            except LiteraturHatasi:
                pass

        q = " ".join(sys.argv[1:]) or "kira sözleşmesi"
        s = await ara(q)
        print(f"'{q}' → {s['toplam']} sonuç ({len(s['kayitlar'])} gösteriliyor)")
        assert s["kayitlar"], "sonuç boş döndü"
        ilk = s["kayitlar"][0]
        print(f"ilk: {ilk['baslik'][:70]} | {ilk['yil']} | {ilk['yer']} | atıf={ilk['atifSayisi']}")
        assert ilk["url"], "kaynak url yok"
        await kapat()

    asyncio.run(_dene())
    print("tamam")
