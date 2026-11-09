"""
UYAP rölesi — yapay zekânın içtihat sorgularını avukatın TELEFONUNDAN geçirir (kullanıcı kararı 2026-10-23).

Ara sekmesi zaten telefonun kendi IP'siyle gidiyor (`/ictihat/plan` → telefon → `/ictihat/coz`). Dilekçe/analiz/
asistan ajanının araç çağrıları ise sunucunun TEK çıkış IP'sini paylaşıyordu: kullanıcı arttıkça UYAP sırası uzar.
Röle açık akışta (`uyapRole: true`) `bedesten.post` içtihat isteğini atmaz; akışa
`{"t":"uyap","oturum","id","url","basliklar","govde"}` yazar, telefon Bedesten'e kendi IP'sinden gider ve ham
yanıtı `POST /uyap/yanit` ile geri yollar. Sunucu yanıtı, kendi çektiği yanıtla AYNI ayrıştırıcıdan geçirir.

Yedek (kalite önce): telefon `ZAMAN_ASIMI_SN` içinde yanıt vermezse ya da hata dönerse `iste()` None döner ve
çağıran sorguyu sunucudan yapar; aynı akışta `MAKS_HATA` başarısızlıktan sonra röle kapanır, kalan sorgular
doğrudan sunucudan gider. Kaynak hiçbir durumda atlanmaz.

KIRMIZI ÇİZGİ (`/ictihat/coz` ile aynı): telefondan gelen yanıt ORTAK önbelleğe yazılmaz — yalnız o akışın
kaynaklarına girer (bkz. bedesten.post, ictihat.karar). Sahte yanıt yalnız sahibinin kendi çıktısını etkiler.

`--workers 1`: bekleyen Future'lar süreç belleğinde. Tek başına:  python server/role.py
"""
import asyncio
import contextlib
import contextvars
import secrets
import time

import olcum

ZAMAN_ASIMI_SN = 60.0  # telefonun kayan penceresi dolarsa (8/30 sn) bekleme + 30 sn fetch sınırı
MAKS_HATA = 3

aktif: contextvars.ContextVar["Role | None"] = contextvars.ContextVar("uyap_role", default=None)
_oturumlar: dict[str, "Role"] = {}
sayac = {"basarili": 0, "yedek": 0}


class Role:
    def __init__(self, sahip: str, bildir) -> None:
        self.id = secrets.token_urlsafe(16)  # 128 bit — yanıt yetkisinin asıl sırrı
        self.sahip = sahip
        self.bildir = bildir
        self.hata = 0
        self.bekleyen: dict[str, asyncio.Future] = {}

    @property
    def acik(self) -> bool:
        return self.hata < MAKS_HATA

    async def iste(self, url: str, basliklar: dict, govde: dict) -> dict | None:
        """Telefonun ham Bedesten yanıtı; alınamazsa None (çağıran sunucudan yapar)."""
        if not self.acik:
            return None
        istek_id = secrets.token_urlsafe(8)
        gelecek = asyncio.get_running_loop().create_future()
        self.bekleyen[istek_id] = gelecek
        t0 = time.monotonic()
        try:
            self.bildir({"t": "uyap", "oturum": self.id, "id": istek_id, "url": url,
                         "basliklar": dict(basliklar), "govde": govde})
            ham = await asyncio.wait_for(gelecek, ZAMAN_ASIMI_SN)
        except asyncio.TimeoutError:
            ham = None
        finally:
            self.bekleyen.pop(istek_id, None)
        if isinstance(ham, dict):
            olcum.kaydet("uyap_role", time.monotonic() - t0)
            sayac["basarili"] += 1
            return ham
        self.hata += 1
        sayac["yedek"] += 1
        return None

    def kapat(self) -> None:
        _oturumlar.pop(self.id, None)
        for g in self.bekleyen.values():
            if not g.done():
                g.cancel()
        self.bekleyen.clear()


@contextlib.contextmanager
def ac(sahip: str, bildir, istendi: bool):
    """Akış boyunca rölesi açık bağlam. `istendi` False ise (eski istemci) hiçbir şey yapmaz."""
    if not istendi:
        yield None
        return
    rol = Role(sahip, bildir)
    _oturumlar[rol.id] = rol
    jeton = aktif.set(rol)
    try:
        yield rol
    finally:
        with contextlib.suppress(ValueError):
            aktif.reset(jeton)
        rol.kapat()


def yanitla(oturum: str, istek_id: str, sahip: str, ham: object) -> bool:
    """Telefonun yanıtı. Bilinmeyen/biten oturum, başkasının oturumu ya da biten istek → False (410).
    `ham` dict değilse (telefon hata bildirdi) bekleyen istek yedeğe düşer."""
    rol = _oturumlar.get(oturum)
    if rol is None or rol.sahip != sahip:
        return False
    gelecek = rol.bekleyen.get(istek_id)
    if gelecek is None or gelecek.done():
        return False
    gelecek.set_result(ham if isinstance(ham, dict) else None)
    return True


if __name__ == "__main__":
    ZAMAN_ASIMI_SN = 0.05

    async def _test():
        olaylar: list[dict] = []
        with ac("u1", olaylar.append, True) as rol:
            assert aktif.get() is rol and rol.id in _oturumlar

            async def telefon(ham):
                while not olaylar:
                    await asyncio.sleep(0)
                o = olaylar.pop()
                assert o["t"] == "uyap" and o["url"] == "https://x/y" and o["govde"] == {"a": 1}
                assert not yanitla(o["oturum"], o["id"], "baskasi", {"data": 1}), "başka kullanıcı yanıtlayamaz"
                assert yanitla(o["oturum"], o["id"], "u1", ham)
                assert not yanitla(o["oturum"], o["id"], "u1", ham), "aynı istek ikinci kez yanıtlanamaz"

            # Başarılı yanıt
            sonuc, _ = await asyncio.gather(rol.iste("https://x/y", {}, {"a": 1}), telefon({"data": {"ok": 1}}))
            assert sonuc == {"data": {"ok": 1}}
            # Telefon hata bildirdi → None (yedek)
            sonuc, _ = await asyncio.gather(rol.iste("https://x/y", {}, {"a": 1}), telefon(None))
            assert sonuc is None and rol.hata == 1
            # Zaman aşımı → None (yedek)
            assert await rol.iste("https://x/y", {}, {"a": 1}) is None and rol.hata == 2
            olaylar.clear()
            assert await rol.iste("https://x/y", {}, {"a": 1}) is None and rol.hata == 3
            # 3 hata → röle kapalı, artık olay bile yazılmaz
            olaylar.clear()
            assert not rol.acik and await rol.iste("https://x/y", {}, {"a": 1}) is None and not olaylar
            oturum = rol.id
        assert aktif.get() is None and oturum not in _oturumlar
        assert not yanitla(oturum, "x", "u1", {}), "kapanan oturum 410"
        with ac("u1", olaylar.append, False) as yok:
            assert yok is None and aktif.get() is None

    asyncio.run(_test())
    assert sayac == {"basarili": 1, "yedek": 3}, sayac

    # Uçtan uca: ictihat.karar → bedesten.post → röle → telefon. Ağ yok; KIRMIZI ÇİZGİ: ortak önbelleğe yazılmaz.
    import base64
    import sys

    sys.modules["role"] = sys.modules["__main__"]  # bedesten/ictihat AYNI ContextVar'ı görsün
    import bedesten
    import ictihat

    async def _uctan_uca():
        olaylar: list[dict] = []
        with ac("u1", olaylar.append, True):
            async def telefon():
                while not olaylar:
                    await asyncio.sleep(0)
                o = olaylar.pop()
                assert o["url"].endswith("/emsal-karar/getDocumentContent") and o["basliklar"]["Origin"]
                yanitla(o["oturum"], o["id"], "u1", {"data": {"mimeType": "text/html",
                                                               "content": base64.b64encode(b"<p>Telefondan karar</p>").decode()}})
            sonuc, _ = await asyncio.gather(ictihat.karar("123"), telefon())
        assert sonuc["metin"].strip() == "Telefondan karar", sonuc
        assert "123" not in ictihat._ONBELLEK and not bedesten._sonuc_onbellek, "röle yanıtı ORTAK ÖNBELLEĞE YAZILMAMALI"
        assert bedesten.sayac["bedesten"] == 0, "sunucu UYAP'a gitmemeli"

    asyncio.run(_uctan_uca())
    print("role uçtan uca (önbelleğe yazmaz, sunucu UYAP'a gitmez): tamam")
    print("role: tamam")
