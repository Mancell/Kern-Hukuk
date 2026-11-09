"""
Süre ölçümü — "yavaşlık yapay zekâdan mı, sunucudan mı?" sorusunun kalıcı cevabı (kullanıcı kararı 2026-10-23).

Bellekte, ölçü başına son 500 süre; içerik/kişi/IP YOK, yalnız saniye. `/istatistik` ortalama + p95 gösterir:
  llm:<model>      tek LLM çağrısının süresi (yapay zekâ)
  uyap_bekleme     UYAP hız kovasında sıra beklemesi (paylaşılan tek çıkış IP'si — sunucu tarafı darboğaz)
  uyap_istek       UYAP'ın kendi yanıt süresi
  kuyruk_bekleme   ağır iş kuyruğunda (PDF/dönüştürme/OCR) sıra beklemesi (sunucu CPU'su)
`--workers 1` olduğu için tek süreç belleği yeterli; yeniden başlayınca sıfırlanır.

Tek başına:  python server/olcum.py
"""
from collections import deque

_ORNEK = 500
_olculer: dict[str, deque] = {}


def kaydet(ad: str, sn: float) -> None:
    _olculer.setdefault(ad, deque(maxlen=_ORNEK)).append(max(0.0, float(sn)))


def ozet() -> dict:
    sonuc = {}
    for ad, d in sorted(_olculer.items()):
        if not d:
            continue
        sirali = sorted(d)
        sonuc[ad] = {
            "adet": len(sirali),
            "ort_sn": round(sum(sirali) / len(sirali), 2),
            "p95_sn": round(sirali[min(len(sirali) - 1, int(len(sirali) * 0.95))], 2),
        }
    return sonuc


if __name__ == "__main__":
    for i in range(1, 101):
        kaydet("deneme", i / 10)
    o = ozet()["deneme"]
    assert o == {"adet": 100, "ort_sn": 5.05, "p95_sn": 9.6}, o
    print("olcum: tamam")
