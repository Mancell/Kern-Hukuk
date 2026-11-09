# Kern-Hukuk

Türk hukuku arama motoru: **içtihat** (Yargıtay, Danıştay, istinaf, yerel, AYM, Uyuşmazlık — UYAP Bedesten),
**mevzuat**, **literatür** (OpenAlex), **AİHM** (HUDOC), **KİK** (EKAP) ve **Resmî Gazete**; isteğe bağlı
yapay zekâ ile cümleden sorgu önerisi ve karar/mevzuat özeti.

LawBombing `server/` içinden çıkarıldı; modüller birebir aynı, yalnız giriş (Supabase JWT) ve kota katmanı yok.

## Kurulum

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # gerekiyorsa doldur
uvicorn api:app --workers 1
```

`--workers 1` şart: UYAP hız kovaları ve telefon rölesinin bekleyen istekleri süreç belleğinde.

## Kütüphane olarak

```python
import sys; sys.path.insert(0, "kern")
import asyncio, ictihat, mevzuat
print(asyncio.run(ictihat.ara("kira tespiti")))
```

Her modülün kendi duman testi var: `python kern/ictihat.py "kira tespiti"`, `python kern/mevzuat.py`,
`python kern/literatur.py`, `python kern/aihm.py`, `python kern/kik.py`, `python kern/resmi_gazete.py`.

| Modül | Kaynak |
|---|---|
| `bedesten.py` | UYAP Bedesten ortak HTTP istemcisi, IP başına hız kovası, proxy havuzu |
| `ictihat.py` | Karar arama + karar metni (PDF yanıtı PyMuPDF ile) |
| `mevzuat.py` | Kanun/yönetmelik arama, tam metin, `madde_bul` |
| `literatur.py` | OpenAlex akademik arama |
| `aihm.py` | HUDOC (curl_cffi — WAF httpx'i engelliyor) |
| `kik.py` | Kamu İhale Kurulu kararları |
| `resmi_gazete.py` | Günlük fihrist + madde metni |
| `role.py`, `olcum.py` | Telefon rölesi, süre ölçümü |
| `llm.py`, `kalite.py`, `sorgu_oneri.py`, `ozet.py` | Yapay zekâ (DeepSeek → Gemini zinciri) |
| `token_kayit.py` | No-op (LawBombing'de Supabase'e yazıyordu) |

## Uçlar

`POST /ictihat/ara|plan|coz|sorgu-oner`, `GET /ictihat/karar/{id}`, `POST /uyap/yanit`,
`POST /mevzuat/ara`, `GET /mevzuat/icerik/{id}`, `POST /literatur/ara`, `POST /aihm/ara|plan|coz`,
`POST /kik/ara|detay`, `POST /resmi-gazete/fihrist|madde`, `GET /ozet/karar/{id}`, `GET /ozet/mevzuat/{id}`.
Şema: `/docs`.

`/plan` + `/coz` "doğrudan erişim" içindir: sunucu sorguyu kurar, istemci Bedesten'e KENDİ IP'sinden gider,
ham yanıtı `/coz`'a verir. `/coz` çıktısı ortak önbelleğe ASLA yazılmaz (sahte karar metni başkasına servis edilmesin).

## Tuzaklar

- **Boşluklu sorgu VEYA sayılır.** `kira tespiti` → 2,2 milyon karar; `"kira tespiti"` → ~2.100. `sorguyu_kur()` bunu düzeltiyor, kaldırmayın.
- **Bedesten hız sınırı ~10 istek / 30 sn / IP.** Tek sunucu IP'si tüm kullanıcılara paylaşılır; proxy havuzu ya da `/plan`+`/coz` ile açılır.
- **Model künye uydurmaz.** Esas/karar/madde no, daire, tarih UYAP'tan geldiği gibi kalır; özetlerde "AI üretimi, doğrulayın" uyarısı gösterin.
- AİHM (HTTP 403) ve KİK (boş sonuç) 2026-11-09'da bu kodun orijinalinde de aynı sonucu verdi — karşı tarafın IP/WAF davranışı, sunucunun çıkış IP'sinden ayrıca denenmeli.

## Kaynaklar

Bedesten istek şekli [saidsurucu/yargi-mcp](https://github.com/saidsurucu/yargi-mcp) (MIT) çözümlemesinden;
OpenAlex adaptörü saidsurucu/literature-review-skills (MIT) skill'inden Python'a taşındı.
