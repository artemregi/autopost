"""Итоги опубликованных рилсов за последние 30 дней: просмотры, досмотр, лайки, сохранения – и какой лучше.
    python stats.py         – отчёт в Telegram
    python stats.py print   – только напечатать
Досмотр – какую часть ролика в среднем смотрят: среднее время просмотра / длина ролика.
Сравнивать честно, когда ролику 2 суток и больше: раньше показы ещё идут.
Флаг --dry-run: ничего не отправлять в сеть, только напечатать запросы (числа будут нулевые)."""
import config  # первым: переключает на окружение .venv
import sys
from datetime import datetime, timedelta

import autopost
import ig_api

DAYS = 30
MIN_AGE = timedelta(days=2)
METRICS = "views,reach,ig_reels_avg_watch_time,saved,shares"


def collect():
    c = ig_api.refresh()
    rows, since = [], datetime.now() - timedelta(days=DAYS)
    for item in autopost.items(autopost.POSTED):
        m = autopost.read_meta(item)
        posted = datetime.fromisoformat(m["posted_at"])
        if posted < since or not m.get("media_id"):
            continue
        r = {"title": m["title"], "posted": posted, "mode": m.get("mode", "trial"), "sec": m.get("seconds") or 0,
             "mature": datetime.now() - posted >= MIN_AGE}
        try:
            ins = ig_api.req("GET", f"/{m['media_id']}/insights", metric=METRICS, access_token=c["token"])["data"]
            r.update({x["name"]: x["values"][0]["value"] for x in ins})
            r.update(ig_api.req("GET", f"/{m['media_id']}", fields="like_count,comments_count", access_token=c["token"]))
        except ig_api.IGError as e:
            r["error"] = str(e)[:120]
        watch = r.get("ig_reels_avg_watch_time", 0) / 1000
        r["hold"] = min(watch / r["sec"], 1) if r["sec"] else 0
        rows.append(r)
    return rows


def line(r):
    if "error" in r:
        return f"  нет данных: {r['error']}"
    return (f"  просмотры {r.get('views', 0)} · досмотр {r['hold']:.0%} · лайки {r.get('like_count', 0)} · "
            f"сохранения {r.get('saved', 0)} · репосты {r.get('shares', 0)}")


def report(rows):
    if not rows:
        return f"За {DAYS} дней опубликованных рилсов нет."
    body = []
    for r in sorted(rows, key=lambda r: -r.get("views", 0)):
        kind = "пробный" if r["mode"] == "trial" else "обычный"
        fresh = "" if r["mature"] else ", ещё набирает"
        body.append(f"«{r['title']}» · {r['posted']:%d.%m} · {kind}{fresh}\n{line(r)}")
    ripe = [r for r in rows if r["mature"] and "error" not in r]
    if len(ripe) >= 2:
        best = max(ripe, key=lambda r: r.get("views", 0))
        hold = max(ripe, key=lambda r: r["hold"])
        summary = (f"Больше всего просмотров: «{best['title']}» ({best.get('views', 0)})\n"
                   f"Лучше всего досматривают: «{hold['title']}» ({hold['hold']:.0%})")
    else:
        summary = "Сравнивать пока рано: нужно хотя бы 2 ролика старше 2 суток."
    return "\n\n".join(body) + "\n\n" + summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    try:
        text = f"Статистика рилсов за {DAYS} дней\n\n" + report(collect())
    except ig_api.IGError as e:
        text = f"⚠️ Статистика рилсов не собралась: {e}"
    if args == ["print"]:
        print(text)
    else:
        autopost.say(text)
