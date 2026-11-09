"""
Kern-Hukuk HTTP API — içtihat, mevzuat, literatür, AİHM, KİK, Resmî Gazete arama + AI özet.

LawBombing `server/app.py`'deki arama uçlarının birebir kopyası; giriş (JWT) ve kota katmanı çıkarıldı.
Kullanıcı kovası anahtarı istemci IP'si. Çalıştırma (TEK işçi şart — hız kovaları süreç belleğinde):

    uvicorn api:app --workers 1
"""
import asyncio
import contextlib
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "kern"))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

import aihm  # noqa: E402
import bedesten  # noqa: E402
import ictihat  # noqa: E402
import kik  # noqa: E402
import literatur  # noqa: E402
import mevzuat  # noqa: E402
import olcum  # noqa: E402,F401
import ozet  # noqa: E402
import resmi_gazete  # noqa: E402
import role  # noqa: E402
import sorgu_oneri  # noqa: E402


@contextlib.asynccontextmanager
async def _yasam(_app: FastAPI):
    yield
    await bedesten.kapat()  # içtihat + mevzuat aynı httpx istemcisini paylaşıyor


app = FastAPI(title="Kern-Hukuk", lifespan=_yasam)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("KERN_CORS", "*").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


ICTIHAT_MAKS_GOVDE = 8 * 1024
ICTIHAT_COZ_MAKS_GOVDE = 6 * 1024 * 1024  # /coz: telefonun getirdiği ham Bedesten yanıtı
_yukleme = asyncio.Semaphore(4)


async def govde_baytlari(istek: Request, sinir: int, mesaj: str) -> bytes:
    """
    İstek gövdesini AKARAK okur ve sınırı aşınca keser.

    `await istek.body()` / `await istek.json()` gövdenin tamamını belleğe alır ve
    uvicorn kendi başına bir sınır uygulamaz — yani kimlik doğrulaması olmayan bir
    uçta gövde boyutu saldırganın seçimidir. Content-Length'e de güvenilmiyor:
    chunked aktarımda hiç gelmiyor.
    """
    # Okumaya ZAMAN AŞIMI: bu fonksiyon `_yukleme` semaforu (4 slot) altında
    # çağrılıyor. Gövdeyi damla damla gönderen 4 istemci, slotları SÜRESİZ
    # tutup `/udf2pdf`, `/ictihat/coz` ve tüm `/extension/*` uçlarını
    # kilitleyebilirdi. Sınır YALNIZ okumada: dönüşüm/OCR kendi süresini
    # kullanmaya devam eder. 120 sn, istemcinin kendi 75 sn'lik sınırının
    # (src/device/convert.ts) çok üstünde — meşru hiçbir yüklemeyi kesmez.
    try:
        async with asyncio.timeout(120):
            ham = b""
            async for parca in istek.stream():
                ham += parca
                if len(ham) > sinir:
                    raise HTTPException(413, mesaj)
            return ham
    except TimeoutError:
        raise HTTPException(408, "Yükleme çok uzun sürdü.")


_ANAHTAR = os.getenv("KERN_ANAHTAR", "")


def anahtar_gerek(istek: Request) -> None:
    """KERN_ANAHTAR tanımlıysa her istek `x-kern-anahtar` başlığında onu taşımalı; tanımsızsa açık."""
    if _ANAHTAR and not secrets.compare_digest(istek.headers.get("x-kern-anahtar", ""), _ANAHTAR):
        raise HTTPException(401, "Bu sunucuyu kullanma yetkiniz yok.")


def kim(istek: Request) -> str:
    """Kullanıcı hız kovasının anahtarı. Giriş katmanı yok → istemci IP'si."""
    return istek.client.host if istek.client else "anon"


# İçtihat arama
# --------------------------------------------------------------------------
# Dönüştürmeden bağımsız bir iş: buraya kullanıcının BELGESİ girmez, yalnızca
# kendi yazdığı arama sorgusu geçer. Bu yüzden dosya boyutu/kuyruk kısıtları
# burada yok; sınırlayan tek şey Bedesten'in hız limiti (bkz. ictihat.py).


@app.post("/ictihat/ara")
async def ictihat_ara(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Arama isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await ictihat.ara(
            sorgu=govde.get("sorgu", ""),
            mahkeme=govde.get("mahkeme", "yargitay"),
            sayfa=govde.get("sayfa", 1),
            baslangic=govde.get("baslangic"),
            bitis=govde.get("bitis"),
            kip=govde.get("kip", "ifade"),
            daire=govde.get("daire") or None,
            kimlik=kullanici_kimligi,
        )
    except ictihat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (ictihat.IctihatHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, ictihat.IctihatHatasi) else "Geçersiz arama.")



# --------------------------------------------------------------------------
# Doğrudan erişim: telefon Bedesten'e KENDİ IP'sinden gider
# --------------------------------------------------------------------------
# /plan sorguyu kurar ve doğrular, /coz ham yanıtı çözer. İkisi de Bedesten'e
# GİTMEZ, kovaya DOKUNMAZ — yani 429 üretemezler. Sunucunun tek çıkış IP'si ve
# `bedesten._kovalar` bu yoldan hiç kullanılmaz; UYAP'ın per-IP sınırı telefon
# başına uygulanır. Eski /ictihat/ara ve /ictihat/karar aynen duruyor (web paneli,
# LLM agent'ı, özet ve eski mobil derlemeler oradan devam eder).


@app.post("/ictihat/plan")
async def ictihat_plan(istek: Request):
    anahtar_gerek(istek)
    kim(istek)
    # Olumlu kanıt: telefon doğrudan yolu kullandıkça bu artar, "bedesten" ARTMAZ.
    # (Yalnız "bedesten 0 kaldı" demek, "hiç arama yapılmadı" ile karışıyordu.)
    bedesten.sayac["plan"] += 1
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Plan isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        if govde.get("tur") == "karar":
            return ictihat.plan_kur_karar(str(govde.get("id") or ""))
        return ictihat.plan_kur(
            sorgu=govde.get("sorgu", ""),
            mahkeme=govde.get("mahkeme", "yargitay"),
            sayfa=govde.get("sayfa", 1),
            baslangic=govde.get("baslangic"),
            bitis=govde.get("bitis"),
            kip=govde.get("kip", "ifade"),
            daire=govde.get("daire") or None,
        )
    except (ictihat.IctihatHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, ictihat.IctihatHatasi) else "Geçersiz arama.")


@app.post("/ictihat/coz")
async def ictihat_coz(istek: Request):
    """
    KIRMIZI ÇİZGİ: bu uç `ictihat._ONBELLEK`'e ve `bedesten._sonuc_onbellek`'e
    ASLA yazmaz; çıktı yalnız çağırana döner. İstemciden gelen karar metni ortak
    önbelleğe girse, bir kullanıcı uydurma esas/karar numarasını diğer avukatlara
    servis ettirebilir (CLAUDE.md: uydurma künye = meslek riski).
    """
    anahtar_gerek(istek)
    kim(istek)
    async with _yukleme:
        ham = await govde_baytlari(istek, ICTIHAT_COZ_MAKS_GOVDE, "Yanıt çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        if govde.get("tur") == "karar":
            return await ictihat.karar_coz(str(govde.get("id") or ""), govde.get("ham"))
        istek_bilgi = govde.get("istek") or {}
        kip = istek_bilgi.get("kip") or "ifade"
        if kip not in ("ifade", "tumu"):
            raise HTTPException(422, "Bilinmeyen arama kipi.")
        veri = (istek_bilgi.get("govde") or {}).get("data") or {}
        if not isinstance(veri, dict):
            raise HTTPException(422, "Geçersiz plan.")
        return ictihat.aramayi_normalize(govde.get("ham"), veri, kip)
    except ictihat.IctihatHatasi as e:
        raise HTTPException(422, str(e))
    except (TypeError, ValueError, AttributeError, KeyError):
        # İstemci JSON'u `.get` zinciriyle okunuyor; şekli bozuksa 500 değil 422.
        raise HTTPException(422, "Geçersiz yanıt.")


@app.post("/uyap/yanit")
async def uyap_yanit(istek: Request):
    """
    UYAP rölesi (server/role.py): telefonun, yapay zekâ akışı için KENDİ IP'sinden çektiği ham Bedesten yanıtı.
    {oturum, id, ham} ya da hata durumunda {oturum, id, hata} — o zaman sunucu sorguyu kendisi yapar.
    KIRMIZI ÇİZGİ (/ictihat/coz ile aynı): yanıt ortak önbelleğe girmez, yalnız o akışın kaynağıdır.
    Oturum akışın sahibine bağlı; bilinmeyen/biten oturum 410.
    """
    anahtar_gerek(istek)
    kullanici = kim(istek)
    async with _yukleme:
        ham = await govde_baytlari(istek, ICTIHAT_COZ_MAKS_GOVDE, "Yanıt çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    if not role.yanitla(str(govde.get("oturum") or ""), str(govde.get("id") or ""), kullanici, govde.get("ham")):
        raise HTTPException(410, "Bu istek artık beklenmiyor.")
    return {"tamam": True}


@app.post("/ictihat/sorgu-oner")
async def ictihat_sorgu_oner(istek: Request):
    """
    Doğal dil → Bedesten sorgusu. Aramadan AYRI uç nokta, iki sebeple:
    arama yolunun gecikmesini artırmamalı, ve model çökse arama çalışmaya
    devam etmeli. İstemci başarısızlıkta ham sorguyla devam ediyor.
    """
    anahtar_gerek(istek)
    kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "İstek çok büyük.")
    try:
        govde = json.loads(ham)
        metin = govde["metin"]
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return {"oneri": await sorgu_oneri.oner(metin)}
    except sorgu_oneri.OneriYok as e:
        raise HTTPException(422, str(e))


@app.get("/ictihat/karar/{belge_id}")
async def ictihat_karar(belge_id: str, istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    try:
        return await ictihat.karar(belge_id, kullanici_kimligi)
    except ictihat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except ictihat.IctihatHatasi as e:
        raise HTTPException(422, str(e))


# --------------------------------------------------------------------------
# Mevzuat arama (kanun / yönetmelik / KHK / tebliğ …)
# --------------------------------------------------------------------------
# İçtihatla AYNI şey: buraya belge girmez, yalnızca arama metni. Aynı bedesten
# çıkış IP'sini (ve hız kovasını) paylaşıyor — bkz. bedesten.py.


@app.post("/mevzuat/ara")
async def mevzuat_ara(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Arama isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await mevzuat.ara(
            sorgu=govde.get("sorgu", ""),
            tur=govde.get("tur") or None,
            no=govde.get("no") or None,
            sayfa=govde.get("sayfa", 1),
            kimlik=kullanici_kimligi,
        )
    except mevzuat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (mevzuat.MevzuatHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, mevzuat.MevzuatHatasi) else "Geçersiz arama.")


@app.get("/mevzuat/icerik/{mevzuat_id}")
async def mevzuat_icerik(mevzuat_id: str, istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    try:
        return await mevzuat.icerik(mevzuat_id, kullanici_kimligi)
    except mevzuat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except mevzuat.MevzuatHatasi as e:
        raise HTTPException(422, str(e))


# --------------------------------------------------------------------------
# Literatür arama (akademik makale/kitap — OpenAlex)
# --------------------------------------------------------------------------
# İçtihat/mevzuatla AYNI gizlilik: buraya belge girmez, yalnızca arama metni.
# Ama BAŞKA arka uç (OpenAlex, bedesten değil) — bkz. literatur.py. Bedesten
# çıkış-IP kovasını paylaşmaz; OpenAlex'in kendi kotası geniştir.


@app.post("/literatur/ara")
async def literatur_ara(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Arama isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await literatur.ara(
            sorgu=govde.get("sorgu", ""),
            sayfa=govde.get("sayfa", 1),
            kimlik=kullanici_kimligi,
        )
    except (literatur.LiteraturHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, literatur.LiteraturHatasi) else "Geçersiz arama.")


@app.post("/aihm/ara")
async def aihm_ara(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Arama isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await aihm.ara(
            sorgu=govde.get("sorgu", ""),
            sayfa=govde.get("sayfa", 1),
            kimlik=kullanici_kimligi,
        )
    except (aihm.AihmHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, aihm.AihmHatasi) else "Geçersiz arama.")


# Doğrudan erişim: telefon HUDOC'a KENDİ IP'sinden gider (bkz. aihm.py başlığı —
# Railway'in IP'si HUDOC tarafından engelleniyor, curl_cffi fingerprint düzeltmesi
# yetmedi). /plan sorguyu kurar/doğrular, /coz istemcinin çektiği ham yanıtı işler;
# ikisi de HUDOC'a GİTMEZ. /aihm/ara (yukarıda) web paneli ve eski derlemeler için duruyor.
@app.post("/aihm/plan")
async def aihm_plan(istek: Request):
    anahtar_gerek(istek)
    kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Plan isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return aihm.plan_kur(sorgu=govde.get("sorgu", ""), sayfa=govde.get("sayfa", 1))
    except (aihm.AihmHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, aihm.AihmHatasi) else "Geçersiz arama.")


@app.post("/aihm/coz")
async def aihm_coz(istek: Request):
    anahtar_gerek(istek)
    kim(istek)
    async with _yukleme:
        ham = await govde_baytlari(istek, ICTIHAT_COZ_MAKS_GOVDE, "Yanıt çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return aihm.coz(govde.get("ham"), int(govde.get("sayfa") or 1))
    except aihm.AihmHatasi as e:
        raise HTTPException(422, str(e))
    except (TypeError, ValueError, AttributeError, KeyError):
        raise HTTPException(422, "Geçersiz yanıt.")


@app.post("/kik/ara")
async def kik_ara(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "Arama isteği çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await kik.ara(
            sorgu=govde.get("sorgu", ""),
            tur=govde.get("tur", "uyusmazlik"),
            kimlik=kullanici_kimligi,
        )
    except kik.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (kik.KikHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, kik.KikHatasi) else "Geçersiz arama.")


@app.post("/kik/detay")
async def kik_detay(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "İstek çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await kik.detay(
            sorgu=govde.get("sorgu", ""),
            tur=govde.get("tur", "uyusmazlik"),
            sira=govde.get("sira", 0),
            karar_no=govde.get("karar_no", ""),
            kimlik=kullanici_kimligi,
        )
    except kik.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (kik.KikHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, kik.KikHatasi) else "Geçersiz istek.")


@app.post("/resmi-gazete/fihrist")
async def resmi_gazete_fihrist(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "İstek çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await resmi_gazete.fihrist_getir(
            tarih=govde.get("tarih", ""),
            mukerrer=govde.get("mukerrer", 0),
            kimlik=kullanici_kimligi,
        )
    except resmi_gazete.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (resmi_gazete.ResmiGazeteHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, resmi_gazete.ResmiGazeteHatasi) else "Geçersiz istek.")


@app.post("/resmi-gazete/madde")
async def resmi_gazete_madde(istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    ham = await govde_baytlari(istek, ICTIHAT_MAKS_GOVDE, "İstek çok büyük.")
    try:
        govde = json.loads(ham)
    except Exception:
        raise HTTPException(400, "Geçersiz istek.")
    if not isinstance(govde, dict):
        raise HTTPException(400, "Geçersiz istek.")
    try:
        return await resmi_gazete.madde_getir(url=govde.get("url", ""), kimlik=kullanici_kimligi)
    except resmi_gazete.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (resmi_gazete.ResmiGazeteHatasi, TypeError, ValueError) as e:
        raise HTTPException(422, str(e) if isinstance(e, resmi_gazete.ResmiGazeteHatasi) else "Geçersiz istek.")


# --------------------------------------------------------------------------
# Yapay zeka özeti — karar / mevzuat (değişmez #2: model GERÇEK metni özetler,
# künye uydurmaz; kod ozet.py'de, ictihat.py DIŞINDA).
# --------------------------------------------------------------------------


@app.get("/ozet/karar/{belge_id}")
async def ozet_karar(belge_id: str, istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    if hazir := ozet.onbellekten("karar", belge_id):
        return hazir   # önbellek isabeti: LLM çağrısı yok, kota sayılmaz
    try:
        return await ozet.karar_ozeti(belge_id, kullanici_kimligi)
    except ictihat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (ozet.OzetHatasi, ictihat.IctihatHatasi) as e:
        raise HTTPException(422, str(e))


@app.get("/ozet/mevzuat/{mevzuat_id}")
async def ozet_mevzuat(mevzuat_id: str, istek: Request):
    anahtar_gerek(istek)
    kullanici_kimligi = kim(istek)
    if hazir := ozet.onbellekten("mevzuat", mevzuat_id):
        return hazir   # önbellek isabeti: LLM çağrısı yok, kota sayılmaz
    try:
        return await ozet.mevzuat_ozeti(mevzuat_id, kullanici_kimligi)
    except mevzuat.CokHizli as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(int(e.sonra) + 1)})
    except (ozet.OzetHatasi, mevzuat.MevzuatHatasi) as e:
        raise HTTPException(422, str(e))
