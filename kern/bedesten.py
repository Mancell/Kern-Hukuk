"""
Bedesten (UYAP Mevzuat arka ucu) — paylaşılan HTTP + hız sınırı katmanı.

`ictihat.py` (emsal karar) ve `mevzuat.py` (kanun/yönetmelik) İKİSİ DE
`bedesten.adalet.gov.tr`'ye gidiyor. UYAP'ın limiti kaynak IP başına
~10 istek/30 sn.

ÇIKIŞ IP'si: varsayılan olarak tek (Railway) — o zaman iki özellik AYNI hız
kovasını paylaşır. Ama proxy havuzu (`ICTIHAT_PROXY_*` env) tanımlıysa istekler
istek-başına round-robin ile birden çok proxy IP'sine dağıtılır ve her IP'nin
KENDİ hız kovası olur (bkz. `_kovalar`). Kova IP başına tutulduğu için hiçbir
tek IP UYAP'ın ~10/30sn per-IP limitini aşmaz; toplam kapasite proxy sayısı
kadar artar. Proxy yoksa davranış eskisiyle birebir aynı (tek kova, tek istemci).

Gizlilik sözleşmesi (ictihat.py / app.py ile aynı):
  - Kullanıcının BELGESİ bu uçlara hiç girmez; dışarı çıkan tek şey arama metni.
    Proxy'ye de yalnızca arama sorgusu gider — belge girmez.
  - Sorgu metni hiçbir log satırına yazılmaz — bilerek tek bir logger çağrısı yok.

Bu dosya expo/react bilmez; tek başına `python server/bedesten.py` ile test edilir.
"""
import asyncio
import contextvars
import hashlib
import json
import os
import time
import urllib.parse
from html.parser import HTMLParser

import httpx

import olcum
import role

TABAN = "https://bedesten.adalet.gov.tr"

# Bedesten UYAP Mevzuat arayüzünden gelmeyen isteklere yanıt vermiyor; bu
# başlıklar tarayıcı isteğini taklit ediyor. Eksiltmeyin, 403 döner.
BASLIKLAR = {
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "AdaletApplicationName": "UyapMevzuat",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}


# --------------------------------------------------------------------------
# Proxy havuzu
# --------------------------------------------------------------------------
# UYAP per-IP limitliyor. Tek çıkış IP'sinde limit tüm kullanıcılarca paylaşılır.
# Proxy havuzu (env) tanımlıysa istekler istek-başına round-robin ile farklı
# proxy IP'lerine dağıtılır; her IP'nin kendi kovası olur (aşağıda). Kimlik
# bilgisi KODA/GIT'e girmez — sadece env (Railway).
_PROXI_HOST = os.getenv("ICTIHAT_PROXY_HOST", "").strip()
_PROXI_USER = os.getenv("ICTIHAT_PROXY_USER", "").strip()
_PROXI_PASS = os.getenv("ICTIHAT_PROXY_PASS", "")
_PROXI_PORTS = os.getenv("ICTIHAT_PROXY_PORTS", "").strip()


def _proxileri_kur() -> list[str | None]:
    """Env'den proxy URL listesi. Eksik/boşsa `[None]` = doğrudan çıkış (eski davranış)."""
    if not (_PROXI_HOST and _PROXI_USER and _PROXI_PASS and _PROXI_PORTS):
        return [None]
    kullanici = urllib.parse.quote(_PROXI_USER, safe="")
    parola = urllib.parse.quote(_PROXI_PASS, safe="")  # `+` gibi karakterler güvenli
    portlar = [p.strip() for p in _PROXI_PORTS.replace("\n", ",").split(",") if p.strip()]
    proxiler = [f"http://{kullanici}:{parola}@{_PROXI_HOST}:{p}" for p in portlar]
    return proxiler or [None]


_PROXILER: list[str | None] = _proxileri_kur()
_sira = 0


def _proxi_sec() -> str | None:
    global _sira
    proxi = _PROXILER[_sira % len(_PROXILER)]
    _sira += 1
    return proxi


class BedestenHatasi(Exception):
    """Kullanıcıya olduğu gibi gösterilebilecek Türkçe hata."""


class CokHizli(BedestenHatasi):
    """Yerel kova doldu; istemci birazdan tekrar denemeli."""

    def __init__(self, sonra: float) -> None:
        self.sonra = sonra
        super().__init__(f"Arama servisi yoğun, {sonra:.0f} saniye sonra tekrar deneyin.")


# --------------------------------------------------------------------------
# Hız sınırı
# --------------------------------------------------------------------------
# Ölçülen Bedesten limiti: kaynak IP başına ~10 istek / 30 sn. Railway'de çıkış
# IP'si TEK, yani bu limit uygulamanın TÜM kullanıcıları arasında paylaşılıyor —
# asıl darboğaz burası, sunucunun CPU'su değil.
#
# ponytail: kova tek süreç belleğinde. `--workers 1` olduğu sürece doğru; worker
# sayısı artarsa sayaç bölünür ve limit aşılır → o noktada Redis.
_ARALIK_SN = float(os.getenv("ICTIHAT_ARALIK_SN", "3.5"))       # jeton başına
_MAKS_BEKLEME_SN = float(os.getenv("ICTIHAT_MAKS_BEKLEME_SN", "8"))

# Kalite önce (kullanıcı kararı 2026-10-23): dilekçe/analiz ajanının araç çağrısı yoğunlukta 8 sn'de pes
# edip kaynağı ATLAMAMALI — atlanan madde/karar dilekçeden sessizce düşüyordu. Bu bağlamda sıra uzun da olsa
# beklenir (kova FIFO). Telefonun içtihat araması gibi etkileşimli çağrılar eski 8 sn'de kalır.
bekleme_siniri: contextvars.ContextVar[float | None] = contextvars.ContextVar("bedesten_bekleme", default=None)


def _bekleme() -> float:
    return bekleme_siniri.get() or _MAKS_BEKLEME_SN


def cikis_sayisi() -> int:
    """Paylaşılan UYAP bütçesini çarpan bağımsız çıkış IP'si sayısı (proxy yoksa 1)."""
    return len(_PROXILER)


class _Kova:
    def __init__(self, aralik_sn: float) -> None:
        self._aralik = aralik_sn
        self._musait = 0.0
        self._ceza_sonu = 0.0   # 429 sonrası: uyuyan rezervasyonlar da bunu bekler
        self._kilit = asyncio.Lock()

    async def jeton(self, maks_bekleme: float) -> None:
        """
        Sıradaki jetonu REZERVE eder, sırası gelene kadar uyur.

        Rezervasyonun kilit içinde ve KÜMÜLATİF olması şart. Önceki hâli
        `_musait`'i yalnızca `simdi >= _musait` iken ileri itiyordu, dolayısıyla
        `_musait` hiçbir zaman `simdi + aralik`'ı aşamıyordu; `bekleme` her turda
        yeniden ≤ aralik ölçülüyor ve `maks_bekleme` eşiği HİÇ AŞILAMIYORDU.
        `CokHizli` ölü koddu, kova yük atmıyordu.

        Ölçüldü (1/10 ölçek, gerçeğe çevrildi): 30 eşzamanlı istekte reddedilen 0,
        sonuncusu 102 sn sonra geçiyordu. İstemci 30 sn'de pes ediyor (bkz.
        `src/device/ictihat.ts`), ama sunucudaki coroutine ölmüyor; sırası gelince
        Bedesten'e gidip kimsenin okumayacağı bir yanıt için PAYLAŞILAN tek çıkış
        IP'sinin jetonunu harcıyordu. Yani yük altında özellik kendi kotasını yer.

        Ayrıca `while` döngüsü yoktu artık: uyanan herkesin yeniden yarıştığı
        yapıda sıra FIFO değildi ve açlık mümkündü. Şimdi sıra kilit alınırken
        belirleniyor (asyncio.Lock bekleyenleri sırayla uyandırır).
        """
        async with self._kilit:
            simdi = time.monotonic()
            an = max(simdi, self._musait)
            bekleme = an - simdi
            if bekleme > maks_bekleme:
                # Rezervasyon YAPMADAN çık — reddedilen istek sıra işgal etmemeli.
                raise CokHizli(bekleme)
            self._musait = an + self._aralik

        # Rezerve edilen ana kadar uyu — ama uyurken CEZA gelmiş olabilir.
        # `cezalandir` yalnızca `_musait`'i ileri itiyordu; zaten rezervasyon almış
        # ve uyuyanlar eski uyanma zamanlarını koruyup Bedesten'e gidiyordu, yani
        # tam da cezanın kapatmak için var olduğu pencerede. Sonuç: paylaşılan tek
        # çıkış IP'sinden ard arda 429'lar ve her seferinde uzayan ceza.
        while True:
            kalan = an - time.monotonic()
            if kalan > 0:
                await asyncio.sleep(kalan)
            if time.monotonic() >= self._ceza_sonu:
                return
            an = self._ceza_sonu

    def cezalandir(self, sn: float) -> None:
        """
        Karşı taraf 429 verdiyse kovayı dondur.

        Hem yeni rezervasyonları (`_musait`) hem UYUYANLARI (`_ceza_sonu`) etkiler.
        `async` değil ve içinde `await` yok — olay döngüsünde atomik, kilit gerekmiyor.
        """
        sonu = time.monotonic() + sn
        self._musait = max(self._musait, sonu)
        self._ceza_sonu = max(self._ceza_sonu, sonu)


# UYAP'ın FİZİKSEL limiti: kaynak IP başına ~10 istek/30 sn. Kova ÇIKIŞ IP'si
# başına tutulur: proxy yoksa tek `None` anahtarı (eski davranış — İÇTİHAT +
# MEVZUAT + tüm kullanıcılar tek kovayı paylaşır); proxy havuzu varsa her proxy
# URL'i kendi kovasını alır, böylece hiçbir tek IP per-IP limiti aşmaz.
# ponytail: sözlük proxy sayısı kadar büyür (≤ port sayısı, sabit); LRU gereksiz.
_kovalar: dict[str | None, _Kova] = {}


def _kova_al(proxi: str | None) -> _Kova:
    kova = _kovalar.get(proxi)
    if kova is None:
        kova = _Kova(_ARALIK_SN)
        _kovalar[proxi] = kova
    return kova

# Kullanıcı başına ADALET kotası. Yukarıdakinden tamamen ayrı bir şey: amacı tek
# kullanıcının paylaşılan kovayı tek başına tüketmesini engellemek.
#
# ponytail: sayaç süreç belleğinde, `--workers 1` şartı zaten `_kova` yüzünden
# vardı. Kota parasal değil, kötüye kullanımı frenlemek için; konteyner yeniden
# başlayınca sıfırlanması kimseye zarar vermiyor. Ücretli plan çıkarsa ya da
# worker sayısı artarsa Postgres'e taşınır.
# Ayrıca KVKK açısından: kullanım verisi hiç yurt dışına çıkmıyor.
_KULLANICI_ARALIK_SN = float(os.getenv("ICTIHAT_KULLANICI_ARALIK_SN", "2.0"))
_kullanici_kovalari: dict[str, _Kova] = {}


def _kullanici_kovasi(kimlik: str) -> _Kova:
    kova = _kullanici_kovalari.get(kimlik)
    if kova is None:
        # ponytail: sözlük sınırsız büyüyor. Kullanıcı başına ~100 bayt, 10.000
        # kullanıcı = 1 MB; konteyner yeniden başlayınca sıfırlanıyor. Gerçek
        # bir sorun olursa LRU'ya çevrilir.
        kova = _Kova(_KULLANICI_ARALIK_SN)
        _kullanici_kovalari[kimlik] = kova
    return kova


# --------------------------------------------------------------------------
# Sonuç önbelleği
# --------------------------------------------------------------------------
# Aynı popüler sorgu (ör. "kira tespiti" Yargıtay sayfa 1) tek jeton harcasın.
# Jeton bu özelliğin EN KIT kaynağı: paylaşılan tek çıkış IP'sinin ~10/30 sn'si.
# İki avukat aynı anda aynı emsali arıyorsa ikincisi kovadan jeton yemez.
#
# Kısa TTL: kararlar/mevzuat listesi teoride değişebilir ama dakikalar içinde
# değişmez; 180 sn "taze sonuç" ile "jeton tasarrufu" arasında güvenli.
# ponytail: süreç belleğinde sade FIFO + TTL. Yeniden başlayınca boşalır, yeter.
_ONBELLEK_TTL_SN = float(os.getenv("BEDESTEN_ONBELLEK_TTL_SN", "180"))
_ONBELLEK_KAPASITE = int(os.getenv("BEDESTEN_ONBELLEK", "300"))
_sonuc_onbellek: dict[str, tuple[float, dict]] = {}


def _onbellek_anahtari(yol: str, govde: dict) -> str:
    ham = yol + "\n" + json.dumps(govde, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(ham.encode("utf-8")).hexdigest()


def _onbellekten(anahtar: str) -> dict | None:
    kayit = _sonuc_onbellek.get(anahtar)
    if kayit is None:
        return None
    ts, veri = kayit
    if time.monotonic() - ts > _ONBELLEK_TTL_SN:
        _sonuc_onbellek.pop(anahtar, None)
        return None
    return veri


def _onbelleke_koy(anahtar: str, veri: dict) -> None:
    if len(_sonuc_onbellek) >= _ONBELLEK_KAPASITE:
        _sonuc_onbellek.pop(next(iter(_sonuc_onbellek)))
    _sonuc_onbellek[anahtar] = (time.monotonic(), veri)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
# Proxy başına ayrı istemci: httpx tek istemcide tek proxy tutuyor. Anahtar
# proxy URL'i (proxy yoksa `None` = doğrudan). ponytail: proxy sayısı sabit.
_istemciler: dict[str | None, httpx.AsyncClient] = {}

# Sunucudan Bedesten'e GERÇEKTEN giden istek sayısı (önbellek vuruşu saymaz).
# /istatistik'te görünür. Tek işi "doğrudan erişim" mobil derlemesinin sunucu
# IP'sini hiç kullanmadığını KANITLAMAK: telefondan arama yap → bu sayı
# değişmemeli. Sadece adet — mevcut _sayac ile aynı gizlilik duruşu.
sayac = {"bedesten": 0, "plan": 0}  # plan: telefonun doğrudan-erişim istekleri (olumlu kanıt)


def _http(proxi: str | None = None) -> httpx.AsyncClient:
    istemci = _istemciler.get(proxi)
    if istemci is None:
        istemci = httpx.AsyncClient(
            base_url=TABAN, headers=BASLIKLAR, timeout=30.0, proxy=proxi
        )
        _istemciler[proxi] = istemci
    return istemci


async def kapat() -> None:
    for istemci in _istemciler.values():
        await istemci.aclose()
    _istemciler.clear()


async def post(yol: str, govde: dict, kimlik: str = "anon", onbelleklenebilir: bool = False) -> dict:
    """
    Bedesten'e POST. Kota + paylaşılan kova + (opsiyonel) sonuç önbelleği.

    `onbelleklenebilir=True` yalnızca IDEMPOTENT okumalar için (arama, içerik):
    önbellekten dönen yanıt HİÇ jeton harcamaz. 429/hatalar önbelleğe girmez.
    """
    if onbelleklenebilir:
        anahtar = _onbellek_anahtari(yol, govde)
        vurus = _onbellekten(anahtar)
        if vurus is not None:
            return vurus

    # Röle (server/role.py): yapay zekâ akışında içtihat isteği avukatın TELEFONUNDAN gider — sunucunun tek çıkış
    # IP'si harcanmaz. Telefonun yanıtı ORTAK ÖNBELLEĞE YAZILMAZ (/ictihat/coz kırmızı çizgisi). Telefon
    # veremezse aşağıdaki sunucu yoluna düşülür (kalite önce, kaynak atlanmaz). Mevzuat bilerek sunucuda:
    # kanun metni herkes için aynı, 24 saat ortak önbellekte (mevzuat._tam_metin).
    rol = role.aktif.get()
    if rol is not None and yol.startswith("/emsal-karar/"):
        ham = await rol.iste(TABAN + yol, BASLIKLAR, govde)
        if ham is not None:
            return ham

    # Bu istek için çıkış IP'sini (proxy) seç. Kova ve istemci ona göre.
    proxi = _proxi_sec()

    # ÖNCE kullanıcı kotası, SONRA o IP'nin UYAP kovası. Sıra bilinçli: tek
    # kullanıcı paylaşılan kovada sıra kapatmadan önce kendi limitinde durmalı,
    # yoksa kotanın koruduğu şeyi tüketmiş olur.
    t0 = time.monotonic()
    if kimlik != "anon":
        await _kullanici_kovasi(kimlik).jeton(_bekleme())
    await _kova_al(proxi).jeton(_bekleme())
    olcum.kaydet("uyap_bekleme", time.monotonic() - t0)
    sayac["bedesten"] += 1
    try:
        t1 = time.monotonic()
        yanit = await _http(proxi).post(yol, json=govde)
        olcum.kaydet("uyap_istek", time.monotonic() - t1)
    except httpx.RequestError:
        # Hatanın kendisini metne koymuyoruz: httpx bazen tam URL'i mesaja
        # gömüyor ve sorgu parametreleri (ve proxy kimliği) kullanıcıya
        # geri yansıyabiliyor.
        raise BedestenHatasi("UYAP servisine ulaşılamadı.")
    if yanit.status_code == 429:
        try:
            sonra = min(60.0, max(1.0, float(yanit.headers.get("Retry-After", "30"))))
        except ValueError:
            sonra = 30.0
        # Yalnız 429 yiyen IP'nin kovasını dondur; diğer proxy'ler çalışmaya devam eder.
        _kova_al(proxi).cezalandir(sonra + 0.5)
        raise CokHizli(sonra)
    if yanit.status_code >= 400:
        raise BedestenHatasi(f"UYAP servisi yanıt vermedi (kod {yanit.status_code}).")
    # JSON olmayan ya da beklenmedik şekilli 200: Bedesten bakım/HTML hata
    # sayfası döndürdüğünde `.json()` ValueError atıyor ve bu işlenmemiş 500
    # olarak kullanıcıya çıkıyordu. Sınıflandırılmış hata, teşhis edilebilir.
    try:
        veri = yanit.json()
    except ValueError:
        raise BedestenHatasi("UYAP yanıtı çözümlenemedi.")
    if not isinstance(veri, dict):
        raise BedestenHatasi("UYAP yanıtı beklenmedik biçimde.")

    # GEÇİCİ BOŞ yanıtı ÖNBELLEĞE ALMA. Bedesten dalgalı: aynı sorgu bazen doğru
    # sonuç, bazen `{"data": null}` dönüyor (canlı gözlendi — 200 OK ama boş).
    # Bunu 180 sn önbelleklersek sağlam bir sorgu üç dakika boyunca "sonuç yok"
    # görünür. `data` dolu olmayan yanıtı geçici hata sayıp önbelleğe koymuyoruz;
    # meşru "0 sonuç" bile dolu bir `data` zarfıyla (total:0) geliyor.
    if onbelleklenebilir and veri.get("data"):
        _onbelleke_koy(anahtar, veri)
    return veri


# --------------------------------------------------------------------------
# HTML → düz metin
# --------------------------------------------------------------------------
class MetneCevir(HTMLParser):
    """
    Bedesten HTML'ini düz metne indirir.

    Emsal kararlarda <br>/<p> dışında yapı yok; ama mevzuat metni Word'den
    dışa aktarılmış HTML ve DEV bir `<style>` bloğu içeriyor. HTMLParser
    `<style>`/`<script>` içeriğini handle_data'ya CDATA olarak yolluyor, yani
    tüm CSS metne sızıyordu. İçerideyken veriyi yok sayıyoruz.
    """

    _ATLA = ("style", "script")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parcalar: list[str] = []
        self._atla_derinlik = 0

    def handle_starttag(self, etiket, ozellikler):
        if etiket in self._ATLA:
            self._atla_derinlik += 1
        elif etiket in ("br", "p", "div", "tr", "li", "ul"):
            self.parcalar.append("\n")

    def handle_endtag(self, etiket):
        if etiket in self._ATLA:
            if self._atla_derinlik:
                self._atla_derinlik -= 1
        elif etiket in ("p", "div", "tr", "li", "ul"):
            self.parcalar.append("\n")

    def handle_data(self, veri):
        if not self._atla_derinlik:
            self.parcalar.append(veri)

    def metin(self) -> str:
        ham = "".join(self.parcalar).replace("\xa0", " ")
        satirlar = [s.strip() for s in ham.split("\n")]
        # Ardışık boş satırları teke indir; kaynak HTML bol <br> içeriyor.
        temiz: list[str] = []
        for s in satirlar:
            if s or (temiz and temiz[-1]):
                temiz.append(s)
        return "\n".join(temiz).strip()


# --------------------------------------------------------------------------
# Duman testi:  python server/bedesten.py
# --------------------------------------------------------------------------
if __name__ == "__main__":
    async def _dene() -> None:
        # HTML→metin çevirisi ağ olmadan doğrulanmalı.
        c = MetneCevir()
        c.feed("<b>8. Ceza Dairesi</b><br><br>SU&Ccedil;<br>: 6136 say&#305;l&#305;<p>Son</p>")
        assert c.metin() == "8. Ceza Dairesi\n\nSUÇ\n: 6136 sayılı\nSon", repr(c.metin())

        # Hız sınırlayıcı gerçekten yük atıyor mu?
        kova = _Kova(0.35)          # gerçek 3,5 sn'nin 1/10'u
        basla = time.monotonic()
        red = 0

        async def _bir() -> None:
            nonlocal red
            try:
                await kova.jeton(0.8)   # gerçek 8 sn'nin 1/10'u
            except CokHizli:
                red += 1

        await asyncio.gather(*[_bir() for _ in range(30)])
        gecen = time.monotonic() - basla
        assert red > 0, "kova yük atmıyor — 30 eşzamanlı istekte hiçbiri reddedilmedi"
        assert gecen < 3.0, f"kabul edilenler çok uzun bekledi ({gecen:.1f} sn)"
        print(f"kova: 30 eşzamanlı istek → {red} reddedildi, {30 - red} geçti, {gecen:.1f} sn")

        # Ceza UYUYAN rezervasyonlara da işlemeli.
        kova2 = _Kova(0.05)
        await kova2.jeton(10)                            # 1. jeton: hemen
        uyuyan = asyncio.create_task(kova2.jeton(10))    # 2. jeton: 0,05 sn sonrasına rezerve
        await asyncio.sleep(0)                           # rezervasyon alınsın
        kova2.cezalandir(0.4)                            # karşı taraf 429 verdi
        basla = time.monotonic()
        await uyuyan
        beklenen = time.monotonic() - basla
        assert beklenen >= 0.35, f"uyuyan rezervasyon cezayı görmedi ({beklenen:.2f} sn)"
        print(f"ceza: uyuyan rezervasyon {beklenen:.2f} sn bekledi (ceza 0,40 sn)")

        # Önbellek: aynı anahtar TTL içinde aynı veriyi döndürür, süre geçince düşer.
        global _ONBELLEK_TTL_SN
        a = _onbellek_anahtari("/x", {"q": "kira"})
        assert _onbellekten(a) is None, "boş önbellekten veri geldi"
        _onbelleke_koy(a, {"sonuc": 1})
        assert _onbellekten(a) == {"sonuc": 1}, "önbellek okunamadı"
        assert _onbellek_anahtari("/x", {"q": "kira"}) == a, "aynı girdi farklı anahtar üretti"
        assert _onbellek_anahtari("/x", {"q": "tahliye"}) != a, "farklı girdi aynı anahtar"
        _eski = _ONBELLEK_TTL_SN
        _ONBELLEK_TTL_SN = -1  # her şey süresi dolmuş sayılsın
        assert _onbellekten(a) is None, "TTL geçmiş kayıt hâlâ dönüyor"
        _ONBELLEK_TTL_SN = _eski
        print("önbellek: TTL ve anahtar tutarlı")

        # Proxy round-robin: istekler sırayla dağıtılmalı.
        global _PROXILER, _sira
        _PROXILER, _sira = ["a", "b"], 0
        secilen = [_proxi_sec() for _ in range(4)]
        assert secilen == ["a", "b", "a", "b"], secilen
        _PROXILER, _sira = [None], 0
        assert _proxi_sec() is None, "proxy yokken doğrudan çıkış"

        # Proxy başına kova İZOLE: bir IP'nin cezası diğerini durdurmamalı.
        ka, kb = _kova_al("a"), _kova_al("b")
        assert ka is not kb and ka is _kova_al("a"), "kova IP başına ve kalıcı"
        ka.cezalandir(0.4)                       # "a" 429 yedi
        basla = time.monotonic()
        await kb.jeton(1.0)                       # "b" gecikmeden geçmeli
        assert time.monotonic() - basla < 0.2, "bir IP'nin cezası diğerini durdurdu"
        _kovalar.clear()
        print("proxy: round-robin + IP başına kova izolasyonu")

        print("\ntamam")

    asyncio.run(_dene())
