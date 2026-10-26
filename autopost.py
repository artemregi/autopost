"""Автопостинг готовых роликов в Instagram: очередь → публикация по одному раз в AUTOPOST_EVERY_MIN минут.
    python autopost.py add <видео> [--caption <файл.txt>] [--cover <картинка>] [--title "<заголовок обложки>"] [--at СЕК]
                                     – поставить ролик в очередь: описание, обложка, превью в Telegram
    python autopost.py list          – что в очереди, что вышло, что не вышло
    python autopost.py remove <N>    – убрать N-й ролик из очереди
    python autopost.py retry         – вернуть всё из failed в очередь
    python autopost.py now           – выложить следующий из очереди сразу, не дожидаясь расписания
    python autopost.py tick          – то, что делает расписание: выложить следующий, если пора
    python autopost.py on [ЧЧ:ММ]    – включить расписание: очередь проверяется каждые 10 минут,
                                       отчёт по статистике – каждый день в ЧЧ:ММ (по умолчанию 10:00)
    python autopost.py off           – выключить расписание
    python autopost.py status        – проверить расписание
Флаг --dry-run: ничего не отправлять в сеть, только напечатать запросы. Работает в отдельной папке dry-run/.

Без --caption описание пишет Claude: расшифровывает ролик и пишет по knowledge/reel_description.md и anketa.md.
Если рядом с видео лежит файл с тем же именем и .txt, описание берётся из него.
Без --cover обложка – кадр из ролика с заголовком (cover.py).

Настройки в .env (все необязательные):
    AUTOPOST_MODE=trial        – trial: пробный рилс (видят не подписчики, в профиль сам не попадает); normal: обычный
    AUTOPOST_EVERY_MIN=60      – минут между публикациями
    AUTOPOST_HOURS=9-22        – выкладывать только с 9:00 до 22:00 по часам компьютера"""
import config  # первым: переключает на окружение .venv
import base64
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import cover
import ig_api
from lib import tg
from lib.common import IS_MAC, IS_WIN, log

DRY = ig_api.DRY
MODE = "normal" if os.getenv("AUTOPOST_MODE", "trial").strip().lower() == "normal" else "trial"
EVERY_MIN = int(os.getenv("AUTOPOST_EVERY_MIN", "60") or 60)
HOURS = tuple(int(x) for x in (os.getenv("AUTOPOST_HOURS", "9-22").strip() or "9-22").split("-"))
TICK_MIN = 10        # как часто расписание проверяет очередь
MAX_ERRORS = 3       # столько неудачных попыток подряд – ролик уходит в failed/
BASE = config.ROOT / "dry-run" if DRY else config.ROOT
QUEUE, POSTED, FAILED = BASE / "queue", BASE / "posted", BASE / "failed"
LOCK = BASE / "data" / "autopost.lock" if DRY else config.DATA / "autopost.lock"
RULES = config.KNOWLEDGE / "reel_description.md"
ANKETA = config.ROOT / "anketa.md"
NAME = "scenarios-autopost"
NO_WINDOW = cover.NO_WINDOW


# ---------- Telegram ----------

def say(text):
    if DRY:
        print(f"[dry-run] Telegram sendMessage:\n{text}\n", flush=True)
        return
    try:
        tg.send(text)
    except Exception as e:
        log("Telegram не ответил:", e)


def say_photo(path, caption):
    if DRY:
        print(f"[dry-run] Telegram sendPhoto {Path(path).name}: {caption}\n", flush=True)
        return
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{config.TG_TOKEN}/sendPhoto", timeout=120,
                          data={"chat_id": config.TG_CHAT, "caption": caption[:1000]}, files={"photo": f})
    except Exception as e:
        log("Telegram не ответил:", e)


# ---------- очередь ----------

def read_meta(item):
    return json.loads((item / "meta.json").read_text(encoding="utf-8"))


def write_meta(item, meta):
    (item / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def items(folder):
    return sorted(p for p in folder.glob("*") if (p / "meta.json").exists()) if folder.exists() else []


def next_number():
    nums = [int(m.group()) for f in (QUEUE, POSTED, FAILED) if f.exists() for p in f.iterdir()
            if (m := re.match(r"\d+", p.name))]
    return max(nums, default=0) + 1


def probe(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(video)],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        j = json.loads(out.stdout)
        s = j["streams"][0]
        return int(s["width"]), int(s["height"]), float(j["format"]["duration"])
    except (ValueError, KeyError, IndexError):
        raise SystemExit(f"❌ Не читается как видео: {video}")


def transcribe(video):
    from lib import speech
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k",
                        str(audio)], check=True, creationflags=NO_WINDOW)
        errors = []
        for name, has_key, fn in speech.ENGINES:
            if not has_key():
                continue
            try:
                return speech.clean(fn(audio)[0])
            except Exception as e:
                errors.append(f"{name}: {str(e)[:150]}")
    raise SystemExit("❌ Не получилось расшифровать ролик: " + "; ".join(errors))


def write_caption(video, title):
    """Описание и заголовок обложки пишет Claude по правилам. → (описание, заголовок обложки)."""
    if DRY:
        print(f"[dry-run] расшифровка {Path(video).name} и claude -p --model {config.WRITE_MODEL} "
              f"(правила {RULES.name}, {ANKETA.name})", flush=True)
        return f"[dry-run] описание к ролику «{title}»\n\n#пример", title
    from lib import llm
    text = transcribe(video)
    anketa = ANKETA.read_text(encoding="utf-8") if ANKETA.exists() else ""
    prompt = (f"{RULES.read_text(encoding='utf-8')}\n\n---\nАнкета автора (тон, ниша, аудитория):\n{anketa}\n\n---\n"
              f"Название ролика: {title}\nТекст ролика (расшифровка):\n{text}\n\n---\n"
              "Напиши описание и заголовок обложки строго по правилам выше. Ответ – только JSON: "
              '{"caption": "описание с хештегами в конце", "cover_title": "заголовок обложки 2–5 слов"}')
    r = llm.ask_json(prompt, config.WRITE_MODEL)
    return r["caption"].strip(), r.get("cover_title", "").strip() or title


def add(video, caption_file=None, cover_img=None, title=None, at=1.0):
    video = Path(video).expanduser().resolve()
    if not video.exists():
        sys.exit(f"❌ Нет такого файла: {video}")
    w, h, sec = probe(video)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", video.stem).strip() or video.stem   # «Тема (ui)» → «Тема»
    side = video.with_suffix(".txt")
    if caption_file:
        caption, cover_title = Path(caption_file).read_text(encoding="utf-8").strip(), title or name
    elif side.exists():
        caption, cover_title = side.read_text(encoding="utf-8").strip(), title or name
    else:
        caption, cover_title = write_caption(video, name)
        cover_title = title or cover_title
    caption = caption.replace("\u2014", "–")   # длинное тире → короткое
    safe = re.sub(r'[\\/:*?"<>|]', "", name)[:60].strip()
    item = QUEUE / f"{next_number():04d} {safe}"   # номер в начале – порядок очереди
    item.mkdir(parents=True)
    try:
        if video.stat().st_size > ig_api.UPLOAD_LIMIT_MB * 1024 * 1024:   # больше, чем берёт временный хостинг
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c:v", "libx264", "-crf", "23",
                            "-preset", "medium", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                            str(item / "video.mp4")], check=True, creationflags=NO_WINDOW)
        else:
            shutil.copy2(video, item / "video.mp4")
        if cover_img:
            cover.to_jpg(cover_img, item / "cover.jpg")
        else:
            cover.make(video, cover_title, item / "cover.jpg", at)
        (item / "caption.txt").write_text(caption + "\n", encoding="utf-8")
        write_meta(item, {"title": name, "source": str(video), "added": datetime.now().isoformat(timespec="seconds"),
                          "seconds": round(sec, 2), "status": "queued", "errors": 0})
    except BaseException:
        shutil.rmtree(item, ignore_errors=True)
        raise
    n = len(items(QUEUE))
    kind = "пробный рилс" if MODE == "trial" else "обычный рилс"
    note = "" if h > w else "\n⚠️ Видео не вертикальное – в Reels будет с полями."
    say_photo(item / "cover.jpg", f"В очереди {n}-м: «{name}» ({sec:.0f} с), {kind}.\n"
                                  f"Выкладываю по одному раз в {EVERY_MIN} мин, с {HOURS[0]}:00 до {HOURS[1]}:00.{note}")
    say(caption)
    print(f"✅ В очереди {n}-м: {item}")
    return item


def last_posted():
    times = [read_meta(p).get("posted_at") for p in items(POSTED)]
    times = [datetime.fromisoformat(t) for t in times if t]
    return max(times) if times else None


# ---------- публикация ----------

def lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime > 40 * 60:   # зависший запуск
        LOCK.unlink(missing_ok=True)
    try:
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL))
        return True
    except FileExistsError:
        return False


def move(item, folder):
    folder.mkdir(parents=True, exist_ok=True)
    return Path(shutil.move(str(item), str(folder / item.name)))


def post(item):
    meta = read_meta(item)
    meta["status"] = "posting"
    write_meta(item, meta)
    caption = (item / "caption.txt").read_text(encoding="utf-8").strip()   # описание можно поправить до публикации
    kind = "пробный рилс" if MODE == "trial" else "рилс"
    try:
        mid, link = ig_api.publish(item / "video.mp4", item / "cover.jpg", caption, trial=MODE == "trial")
    except Exception as e:
        meta.update(status="queued", errors=meta.get("errors", 0) + 1, last_error=str(e)[:500])
        write_meta(item, meta)
        log("Не вышел", meta["title"], e)
        if meta["errors"] >= MAX_ERRORS:
            move(item, FAILED)
            say(f"⚠️ Автопостинг: «{meta['title']}» не вышел {MAX_ERRORS} раза подряд, убрал в папку failed.\n{e}")
        else:
            say(f"⚠️ Автопостинг: «{meta['title']}» не вышел, попробую ещё раз через {TICK_MIN} мин.\n{e}")
        return False
    meta.update(status="posted", media_id=mid, permalink=link, mode=MODE,
                posted_at=datetime.now().isoformat(timespec="seconds"))
    write_meta(item, meta)
    move(item, POSTED)
    left = len(items(QUEUE))
    log("Вышел", meta["title"], link)
    say(f"Вышел {kind} «{meta['title']}»: {link}\nВ очереди осталось: {left}")
    return True


def tick(force=False):
    if not lock():
        print("Уже идёт публикация – жду следующего запуска")
        return
    try:
        for item in items(QUEUE):   # запуск оборвался посреди публикации: повторять вслепую нельзя, мог выйти дубль
            if read_meta(item).get("status") == "posting":
                move(item, FAILED)
                say(f"⚠️ Автопостинг: публикация «{read_meta(FAILED / item.name)['title']}» оборвалась. Проверь в Instagram, "
                    "вышел ли ролик. Если нет – попроси Claude запустить autopost.py retry.")
        queue = items(QUEUE)
        if not queue:
            print("Очередь пустая")
            return
        if not force:
            if not HOURS[0] <= datetime.now().hour < HOURS[1]:
                print(f"Сейчас не публикую: окно с {HOURS[0]}:00 до {HOURS[1]}:00")
                return
            last = last_posted()
            if last and datetime.now() - last < timedelta(minutes=EVERY_MIN):
                wait = last + timedelta(minutes=EVERY_MIN)
                print(f"Следующий – не раньше {wait:%H:%M}")
                return
        post(queue[0])
    finally:
        LOCK.unlink(missing_ok=True)


def show():
    print(f"Режим: {'пробные рилсы' if MODE == 'trial' else 'обычные рилсы'}, раз в {EVERY_MIN} мин, "
          f"с {HOURS[0]}:00 до {HOURS[1]}:00")
    q = items(QUEUE)
    print(f"\nВ очереди ({len(q)}):")
    for i, p in enumerate(q, 1):
        m = read_meta(p)
        err = f" · ошибок: {m['errors']} ({m.get('last_error', '')[:80]})" if m.get("errors") else ""
        print(f"  {i}. {m['title']} · {m['seconds']:.0f} с · добавлен {m['added'][:16].replace('T', ' ')}{err}")
    done = items(POSTED)[-10:]
    print(f"\nВышли (последние {len(done)}):")
    for p in done:
        m = read_meta(p)
        print(f"  {m['posted_at'][:16].replace('T', ' ')} · {m['title']} · {m.get('permalink', '')}")
    bad = items(FAILED)
    if bad:
        print(f"\nНе вышли, папка failed ({len(bad)}):")
        for p in bad:
            m = read_meta(p)
            print(f"  {p.name} · {m.get('last_error', 'публикация оборвалась')[:120]}")


def retry():
    """Всё из failed – обратно в очередь (после того как проверил в Instagram, что ролики не вышли)."""
    for p in items(FAILED):
        m = read_meta(p)
        m.update(status="queued", errors=0)
        write_meta(p, m)
        move(p, QUEUE)
        print(f"Вернул в очередь: {m['title']}")


def remove(n):
    q = items(QUEUE)
    if not 1 <= n <= len(q):
        sys.exit(f"❌ В очереди {len(q)} роликов, номера {n} нет")
    title = read_meta(q[n - 1])["title"]
    shutil.rmtree(q[n - 1])
    print(f"Убрал из очереди: {title}")


# ---------- расписание ----------

def _plist(label):
    return Path.home() / "Library" / "LaunchAgents" / f"com.{label}.plist"


def _ps(script):
    # закодированная команда: кавычки и кириллица в пути (C:\Users\Ира\...) не ломают PowerShell
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                       capture_output=True)
    if r.returncode != 0:
        sys.exit("Не получилось: " + r.stderr.decode("utf-8", "replace")[-500:])


def on(at="10:00"):
    h, m = (int(x) for x in at.split(":"))
    me, stats = Path(__file__).resolve(), config.ROOT / "stats.py"
    if IS_MAC:
        # у фонового запуска нет твоего терминала: явно говорим, где лежат ffmpeg и python
        dirs = [str(Path(p).parent) for p in (shutil.which("ffmpeg"), sys.executable) if p]
        path = ":".join(dict.fromkeys(dirs + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
        logf = str(config.LOGS / "autopost.log")
        jobs = {NAME: {"ProgramArguments": [sys.executable, "-u", str(me), "tick"], "StartInterval": TICK_MIN * 60},
                NAME + "-stats": {"ProgramArguments": [sys.executable, "-u", str(stats)],
                                  "StartCalendarInterval": {"Hour": h, "Minute": m}}}
        for label, job in jobs.items():
            p = _plist(label)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(plistlib.dumps({"Label": f"com.{label}", "WorkingDirectory": str(config.ROOT),
                                          "StandardOutPath": logf, "StandardErrorPath": logf,
                                          "EnvironmentVariables": {"PATH": path}, **job}))
            subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
            subprocess.run(["launchctl", "load", str(p)], check=True)
    elif IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")   # без чёрного окна каждые 10 минут
        exe = pyw if pyw.exists() else Path(sys.executable)
        q = lambda v: str(v).replace("'", "''")  # апостроф в пути для PowerShell удваивается
        settings = ("$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
                    "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1);")
        _ps("$ProgressPreference = 'SilentlyContinue';" + settings +
            f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(me)}\" tick' -WorkingDirectory '{q(config.ROOT)}';"
            f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes {TICK_MIN}) "
            "-RepetitionDuration (New-TimeSpan -Days 3650);"
            f"Register-ScheduledTask -TaskName '{NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
            f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(stats)}\"' -WorkingDirectory '{q(config.ROOT)}';"
            f"$t = New-ScheduledTaskTrigger -Daily -At '{at}';"
            f"Register-ScheduledTask -TaskName '{NAME}-stats' -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    else:
        sys.exit("Поддерживаются macOS и Windows")
    print(f"Включено: очередь проверяется каждые {TICK_MIN} мин, ролик выходит раз в {EVERY_MIN} мин "
          f"с {HOURS[0]}:00 до {HOURS[1]}:00, отчёт по статистике – каждый день в {at}. Компьютер должен быть включён.")


def off():
    for label in (NAME, NAME + "-stats"):
        if IS_MAC:
            subprocess.run(["launchctl", "unload", str(_plist(label))], capture_output=True)
            _plist(label).unlink(missing_ok=True)
        elif IS_WIN:
            subprocess.run(["schtasks", "/Delete", "/TN", label, "/F"], capture_output=True)
    print("Выключено. Очередь осталась на месте.")


def status():
    for label, what in ((NAME, "Публикация из очереди"), (NAME + "-stats", "Отчёт по статистике")):
        if IS_MAC:
            ok = f"com.{label}" in subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        elif IS_WIN:
            ok = subprocess.run(["schtasks", "/Query", "/TN", label], capture_output=True).returncode == 0
        else:
            ok = False
        print(f"{what}: {'включено' if ok else 'выключено'}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    cmd = args[0] if args else ""
    if cmd == "add" and len(args) >= 2:
        opts, rest = {}, []
        it = iter(args[1:])
        for a in it:
            if a in ("--caption", "--cover", "--title", "--at"):
                opts[a[2:]] = next(it, "")
            else:
                rest.append(a)
        add(rest[0], opts.get("caption"), opts.get("cover"), opts.get("title"), float(opts.get("at") or 1.0))
    elif cmd == "list":
        show()
    elif cmd == "remove" and len(args) == 2:
        remove(int(args[1]))
    elif cmd == "retry":
        retry()
    elif cmd == "now":
        tick(force=True)
    elif cmd == "tick":
        tick()
    elif cmd == "on":
        on(*args[1:2])
    elif cmd == "off":
        off()
    elif cmd == "status":
        status()
    else:
        print(__doc__)
