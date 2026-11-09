"""
AİHM (Avrupa İnsan Hakları Mahkemesi) karar arama — HUDOC.

`literatur.py`'nin kardeşi: UYAP Bedesten DEĞİL, `hudoc.echr.coe.int` — Avrupa
Konseyi'nin ücretsiz, anahtarsız karar veritabanı. Bedesten'in paylaşılan
çıkış-IP kovasını KULLANMAZ; kendi istemcisi var.

`httpx` DEĞİL, `curl_cffi` (2026-09-15 ölçüldü): HUDOC'un WAF'ı `httpx`'in TLS
el sıkışma imzasını (JA3) bot olarak işaretleyip HER isteğe HTTP 403 veriyordu
— aynı istek `curl`/gerçek tarayıcıdan sorunsuz geçiyordu, yani engel User-Agent
değil bağlantının kendisiydi. `curl_cffi` gerçek Chrome TLS/HTTP2 parmak izini
taklit ediyor (`impersonate="chrome124"`), sorunu ortadan kaldırıyor.

DOĞRUDAN ERİŞİM (2026-09-16 eklendi): `curl_cffi` fingerprint'i düzeltince bile
Railway'in ÇIKIŞ IP'si HUDOC'tan HTTP 403 almaya devam etti — bir gece boyunca
tekrar tekrar ölçüldü, kendiliğinden açılmadı. Yani engel fingerprint DEĞİL,
IP itibarı (muhtemelen "bulut/datacenter" kovası) — `ictihat.py`/`bedesten.py`'nin
UYAP için zaten çözdüğü sorunun aynısı. Aynı çözüm: istek artık TELEFONUN KENDİ
IP'sinden atılıyor (`/aihm/plan` → `bedesten.ts`nin GET kardeşi → `/aihm/coz`).
`plan_kur()` sorguyu doğrulayıp tam HUDOC URL'ini kurar (Lucene kaçışı burada —
istemciye GÖMÜLMEZ), `coz()` istemcinin çektiği ham JSON'u `ara()`'nın döndürdüğü
şekle çevirir. `ara()` (curl_cffi ile sunucudan giden eski yol) hâlâ duruyor —
web paneli ve doğrudan yol çalışmayan cihazlar için düşülecek yer.

SORGU CANLI: her arama HUDOC'a o an gidiyor, hiçbir şey önceden çekilip
saklanmıyor (CSV/anlık görüntü YOK — kullanıcı kararı: "yeni kararları da
görsün"). Bu yüzden tazelik sorunu YAPISAL OLARAK yok; Bedesten/mevzuat.py ile
aynı ilke (`CLAUDE.md`: "arama sorgusu ÇIKAR, belge asla saklanmaz").

SÖZLEŞME CANLI ÖLÇÜLDÜ (2026-09-14), kullanıcının HUDOC arayüzünden aldığı
RSS adresinden yola çıkılıp gerçek JSON API'ye ulaşıldı:
  - `GET /app/query/results?query=<Lucene>&select=<alanlar>&sort=<alan> Descending&start=<ofset>&length=<adet>`
    → `{"resultcount": int, "results": [{"columns": {...}}], "message": null}`.
  - `sort=kpdate Descending` DOĞRULANDI (tarihe göre azalan) — `kpdateDescending`
    (boşluksuz) SESSİZCE YOK SAYILIYOR ve rastgele sıra dönüyor; ÖLÇÜLMEDEN
    kullanılsaydı "yeni kararlar" hiç güvenilir olmazdı.
  - Serbest metin + sabit filtreler BİRLİKTE çalışıyor: `... AND (ifade özgürlüğü)`
    618 sonuç, doğru sıralı, verildi — ayrı bir "metin alanı" gerekmiyor, çıplak
    kelimeler varsayılan alanda aranıyor.
  - `select` ile zengin künye geliyor: `conclusion` (madde madde ihlal/ihlal-yok),
    `article`, `importance`, `respondent`, `doctype`, `kpdate` — MODEL BUNLARI
    UYDURMUYOR, doğrudan HUDOC'tan.
  - TAM METİN bu API'den gelmiyor: `/app/conversion/docx/` (GET 405, POST 404),
    `/app/query/content`, `/app/documents/<id>` denendi, hiçbiri ham metin
    vermedi — viewer sayfası (`/eng?i=<id>`) JS ile render eden bir SPA kabuğu.
    v1 bu yüzden yalnız ARAMA + zengin künye + HUDOC'un kendi bağlantısı;
    UYAP evrak görüntülemedeki "panel UYAP'ın kendi adresine bağlantı çizer"
    ilkesiyle aynı — tam metin scraping/AI özeti AYRI bir iş (v2).

Gizlilik: buraya belge girmez, yalnızca avukatın yazdığı arama metni HUDOC'a
gider (üçüncü taraf, Avrupa Konseyi). Loglanmaz.

Tek başına çalışır:
    python server/aihm.py "ifade özgürlüğü"
"""
import re
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

BASE = "https://hudoc.echr.coe.int"
SAYFA_BOYU = 20
_SORGU_MAKS = 200
# Lucene sorgu diline kaçış YOK: parantez/tırnak kaldırılan sorgu tek bir
# `AND (...)` grubunun İÇİNE giriyor. Kullanıcı `)` yazabilseydi kendi grubunu
# erken kapatıp bizim sabit filtrelerimizin (dil/koleksiyon) dışına taşabilirdi
# — mevzuat.py'nin dar beyaz listesiyle aynı gerekçe, farklı tehdit.
_KOTU = re.compile(r'[\x00-\x1f\x7f()"]')

# HUDOC'un KENDİ arayüzünün ürettiği sabit filtreler (kullanıcının verdiği
# HUDOC bağlantısından alındı, uydurulmadı): basın açıklamaları ve eski
# komite kayıtları dışlanır; yalnız Türkçe çeviri VE Büyük Daire/Daire
# kararları — komite/kabul edilebilirlik kararları kapsam dışı (v1'de).
_SABIT_FILTRE = (
    'contentsitename:ECHR AND (NOT (doctype=PR OR doctype=HFCOMOLD OR doctype=HECOMOLD)) '
    'AND ((languageisocode="TUR")) '
    'AND ((documentcollectionid="GRANDCHAMBER") OR (documentcollectionid="CHAMBER"))'
)
_ALANLAR = "itemid,docname,kpdate,doctype,respondent,article,conclusion,importance,originatingbody"

_istemci: AsyncSession | None = None


class AihmHatasi(Exception):
    """Arama başarısız. Çağıran kullanıcıya gösterilebilir mesaja çevirir."""


def _get() -> AsyncSession:
    global _istemci
    if _istemci is None:
        _istemci = AsyncSession(base_url=BASE, timeout=20.0, impersonate="chrome124")
    return _istemci


async def kapat() -> None:
    global _istemci
    if _istemci is not None:
        await _istemci.close()
        _istemci = None


def _sorgu_temizle(sorgu: str) -> str:
    if len(sorgu) > _SORGU_MAKS:
        raise AihmHatasi("Arama metni çok uzun.")
    temiz = _KOTU.sub(" ", sorgu).strip()
    temiz = re.sub(r"\s+", " ", temiz)
    if not temiz:
        raise AihmHatasi("Arama metni boş olamaz.")
    return temiz


def _onem_metni(kod: str | None) -> str | None:
    """HUDOC 'importance' 1 (en önemli) – 4 (düşük) arası bir kod; künyeye
    çevrilmezse rakam avukata bir şey anlatmıyor."""
    return {
        "1": "Anahtar dava", "2": "Yüksek önem", "3": "Orta önem", "4": "Düşük önem",
    }.get(str(kod or "").strip())


def _karar(k: dict) -> dict:
    c = k.get("columns") or {}
    itemid = str(c.get("itemid") or "").strip()
    return {
        "id": itemid,
        "baslik": (c.get("docname") or "").strip() or "(başlıksız)",
        "davali": c.get("respondent"),
        "tarih": (c.get("kpdate") or "")[:10] or None,
        "maddeler": [m for m in (c.get("article") or "").split(";") if m],
        "sonuc": c.get("conclusion"),
        "onem": _onem_metni(c.get("importance")),
        # Tam metin bu API'den gelmiyor (bkz. modül başlığı) — HUDOC'un kendi
        # görüntüleyicisine bağlanıyoruz, UYAP evrak görüntülemedeki ilkenin aynısı.
        "url": f"{BASE}/eng?i={itemid}" if itemid else None,
    }


def plan_kur(sorgu: str = "", sayfa: int = 1) -> dict:
    """Sorguyu doğrular/kurar, tam HUDOC URL'ini döner — istemci bu URL'i
    KENDİ IP'sinden GET'ler (Lucene kaçışı burada, istemciye gömülmez).
    `coz()` istemcinin çektiği ham yanıtı işler."""
    temiz = _sorgu_temizle((sorgu or "").strip())
    sayfa = max(1, min(int(sayfa), 100))
    baslangic = (sayfa - 1) * SAYFA_BOYU
    params = {
        "query": f"{_SABIT_FILTRE} AND ({temiz})",
        "select": _ALANLAR,
        "sort": "kpdate Descending",
        "start": str(baslangic),
        "length": str(SAYFA_BOYU),
    }
    return {"url": f"{BASE}/app/query/results?{urlencode(params)}", "sayfa": sayfa}


def coz(ham: object, sayfa: int) -> dict:
    if not isinstance(ham, dict):
        raise AihmHatasi("AİHM yanıtı çözümlenemedi.")
    kayitlar = [_karar(k) for k in (ham.get("results") or []) if (k.get("columns") or {}).get("itemid")]
    return {
        "toplam": ham.get("resultcount") or 0,
        "sayfa": sayfa,
        "kaynak": "AİHM / HUDOC",
        "kayitlar": kayitlar,
    }


async def ara(sorgu: str = "", sayfa: int = 1, kimlik: str = "anon") -> dict:
    """AİHM (Türkçe çevirili, Büyük Daire/Daire) serbest-metin araması —
    SUNUCUDAN gider (`curl_cffi`). Doğrudan yol (telefon kendi IP'sinden,
    bkz. modül başlığı) tercih edilir; bu yalnız düşülecek eski yol.

    `kimlik` imza simetrisi için var (HUDOC per-kullanıcı kovası yok, kullanılmıyor).
    """
    plan = plan_kur(sorgu, sayfa)
    try:
        yanit = await _get().get(plan["url"])
    except RequestException:
        raise AihmHatasi("AİHM (HUDOC) servisine ulaşılamadı.")
    if yanit.status_code == 429:
        raise AihmHatasi("AİHM arama servisi yoğun; biraz sonra tekrar deneyin.")
    if yanit.status_code != 200:
        raise AihmHatasi(f"AİHM araması başarısız (HTTP {yanit.status_code}).")
    try:
        veri = yanit.json()
    except Exception:
        raise AihmHatasi("AİHM yanıtı çözümlenemedi.")
    return coz(veri, plan["sayfa"])


# --------------------------------------------------------------------------
# Duman testi:  python server/aihm.py "ifade özgürlüğü"
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import sys

    # Sorgu temizleme + künye normalize: ağ gerekmez, asıl kırılgan yer burası.
    # `(`/`)`/`"` sorgu grubunun DIŞINA taşıp sabit filtreleri (dil/koleksiyon)
    # ezebilirdi — kullanıcı bunları yazarsa sessizce süzülmeli.
    saldiri = 'kira") OR (languageisocode=*'
    assert _sorgu_temizle(saldiri) == "kira OR languageisocode=*"
    assert "(" not in _sorgu_temizle(saldiri) and ")" not in _sorgu_temizle(saldiri)
    assert _onem_metni("1") == "Anahtar dava" and _onem_metni(None) is None
    k = _karar({"columns": {
        "itemid": "001-1", "docname": "CASE OF X v. TÜRKİYE", "respondent": "TUR",
        "kpdate": "2026-05-05T00:00:00", "article": "3;5;5-1", "importance": "4",
        "conclusion": "Violation of Article 3",
    }})
    assert k["id"] == "001-1" and k["tarih"] == "2026-05-05" and k["maddeler"] == ["3", "5", "5-1"]
    assert k["onem"] == "Düşük önem" and k["url"] == f"{BASE}/eng?i=001-1"

    # Doğrudan erişim: plan_kur ağsız kurulur, coz ağsız çözülür.
    plan = plan_kur("kira", sayfa=2)
    assert plan["url"].startswith(f"{BASE}/app/query/results?") and "kira" in plan["url"] and plan["sayfa"] == 2
    ornek = {"resultcount": 5, "results": [{"columns": {"itemid": "001-2", "docname": "X"}}]}
    sonuc = coz(ornek, 2)
    assert sonuc["toplam"] == 5 and sonuc["sayfa"] == 2 and sonuc["kayitlar"][0]["id"] == "001-2"
    try:
        plan_kur("", 1)
        raise AssertionError("boş sorgu plan_kur'dan geçti")
    except AihmHatasi:
        pass
    print("birim: tamam")

    async def _dene() -> None:
        for kotu in ("", "x" * 300):
            try:
                await ara(kotu)
                raise AssertionError(f"geçersiz sorgu geçti: {kotu[:8]!r}")
            except AihmHatasi:
                pass

        q = " ".join(sys.argv[1:]) or "ifade özgürlüğü"
        s = await ara(q)
        print(f"'{q}' → {s['toplam']} sonuç ({len(s['kayitlar'])} gösteriliyor)")
        assert s["kayitlar"], "sonuç boş döndü"
        ilk = s["kayitlar"][0]
        print(f"ilk: {ilk['baslik'][:70]} | {ilk['tarih']} | {ilk['onem']} | maddeler={ilk['maddeler']}")
        assert ilk["url"] and ilk["url"].startswith(BASE), "kaynak url yok"
        # İkinci sayfa FARKLI sonuç vermeli (sayfalama gerçekten çalışıyor mu)
        s2 = await ara(q, sayfa=2)
        if s2["kayitlar"]:
            assert s2["kayitlar"][0]["id"] != ilk["id"], "2. sayfa 1. sayfayla aynı — sayfalama kırık"
        await kapat()

    asyncio.run(_dene())
    print("tamam")
