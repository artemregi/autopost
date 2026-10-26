"""Официальный Instagram API (вход через Instagram, Meta-приложение). Токен хранится в data/instagram.json.
    python ig_api.py token <токен>   – сохранить токен и проверить, чей он
    python ig_api.py me              – проверить токен: имя аккаунта и тип
    python ig_api.py refresh         – продлить токен ещё на 60 дней прямо сейчас
Токен живёт 60 дней. Каждая публикация и отчёт продлевают его сами, если с прошлого продления прошла неделя.
Instagram скачивает видео сам по публичной ссылке, поэтому файл на время кладётся на uguu.se (удаляется сам через 3 часа).
Флаг --dry-run: ничего не отправлять в сеть, только напечатать запросы."""
import config  # первым: переключает на окружение .venv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = "https://graph.instagram.com/v23.0"
CONF = config.DATA / "instagram.json"
UPLOAD = "https://uguu.se/upload"
UPLOAD_LIMIT_MB = 120   # uguu.se принимает до 128 МБ
DRY = "--dry-run" in sys.argv


class IGError(Exception):
    pass


def _show(params):
    return {k: ("***" if k == "access_token" else v) for k, v in params.items()}


def _fake(method, path, params):
    """Ответ вместо настоящего в --dry-run: форма как у Instagram, значения пустые."""
    if path.endswith("/me"):
        return {"user_id": "DRY_USER", "username": "dry_run", "account_type": "BUSINESS"}
    if "refresh_access_token" in path:
        return {"access_token": params["access_token"], "expires_in": 5184000}
    if path.endswith("/media"):
        return {"id": "DRY_CONTAINER"}
    if path.endswith("/media_publish"):
        return {"id": "DRY_MEDIA"}
    if path.endswith("/insights"):
        return {"data": [{"name": m, "values": [{"value": 0}]} for m in params["metric"].split(",")]}
    if "status_code" in params.get("fields", ""):
        return {"status_code": "FINISHED", "status": "Finished"}
    return {"id": "DRY_MEDIA", "permalink": "https://www.instagram.com/reel/DRY_RUN/", "like_count": 0,
            "comments_count": 0}


def req(method, path, **params):
    url = path if path.startswith("http") else BASE + path
    if DRY:
        print(f"[dry-run] {method} {url} {json.dumps(_show(params), ensure_ascii=False)}", flush=True)
        return _fake(method, url, params)
    try:
        r = requests.request(method, url, params=params if method == "GET" else None,
                             data=params if method == "POST" else None, timeout=120)
    except requests.RequestException as e:
        raise IGError(f"Instagram не ответил: {e}") from None
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or "error" in j:
        err = j.get("error") or {}
        raise IGError(f"Instagram API {r.status_code}: {err.get('message') or r.text[:300]}")
    return j


def conf():
    if DRY and not CONF.exists():
        return {"token": "DRY_TOKEN", "user_id": "DRY_USER", "username": "dry_run",
                "refreshed": datetime.now().isoformat(timespec="seconds")}
    if not CONF.exists():
        raise IGError("Нет токена Instagram: python ig_api.py token <токен>")
    return json.loads(CONF.read_text(encoding="utf-8"))


def _save(c):
    if DRY:
        print("[dry-run] токен не сохраняю", flush=True)
        return
    CONF.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def save_token(token):
    me = req("GET", "/me", fields="user_id,username,account_type", access_token=token)
    _save({"token": token, "user_id": str(me["user_id"]), "username": me.get("username", ""),
           "refreshed": datetime.now().isoformat(timespec="seconds")})
    return me


def refresh(force=False):
    """Продлить токен, если с прошлого продления прошла неделя. → настройки с действующим токеном."""
    c = conf()
    if not force and datetime.now() - datetime.fromisoformat(c["refreshed"]) < timedelta(days=7):
        return c
    r = req("GET", "https://graph.instagram.com/refresh_access_token", grant_type="ig_refresh_token",
            access_token=c["token"])
    c.update(token=r["access_token"], refreshed=datetime.now().isoformat(timespec="seconds"))
    _save(c)
    return c


def public_url(path):
    """Временная публичная ссылка на файл: Instagram забирает видео и обложку сам по ссылке."""
    path = Path(path)
    if DRY:
        print(f"[dry-run] POST {UPLOAD} files[]={path.name} ({path.stat().st_size / 1e6:.1f} МБ)", flush=True)
        return f"https://uguu.example/{path.name}"
    answer = ""
    try:
        with open(path, "rb") as f:
            r = requests.post(UPLOAD, files={"files[]": (path.name, f)}, timeout=900)
        answer = r.text[:200]
        return r.json()["files"][0]["url"]
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        raise IGError(f"Не загрузился {path.name} на uguu.se: {answer or e}") from None


def publish(video, cover, caption, trial=True):
    """Рилс в Instagram. trial=True – пробный: видят только не подписчики, в профиль сам не попадает.
    → (id публикации, ссылка)."""
    c = refresh()
    tok, uid = c["token"], c["user_id"]
    params = dict(media_type="REELS", video_url=public_url(video), caption=caption[:2200], access_token=tok)
    if trial:
        params["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
    else:
        params["share_to_feed"] = "true"
    if cover:
        params["cover_url"] = public_url(cover)
    cid = req("POST", f"/{uid}/media", **params)["id"]
    for _ in range(40):   # Instagram обрабатывает видео обычно 1–3 минуты
        st = req("GET", f"/{cid}", fields="status_code,status", access_token=tok)
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise IGError(f"Instagram не принял видео: {st.get('status')}")
        time.sleep(0 if DRY else 15)
    else:
        raise IGError("Instagram обрабатывает видео дольше 10 минут")
    mid = req("POST", f"/{uid}/media_publish", creation_id=cid, access_token=tok)["id"]
    try:
        link = req("GET", f"/{mid}", fields="permalink", access_token=tok).get("permalink", "")
    except IGError:
        link = ""   # рилс уже вышел – ссылка не главное
    return str(mid), link


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    try:
        if len(args) == 2 and args[0] == "token":
            me = save_token(args[1].strip())
            print(f"✅ Токен сохранён: @{me.get('username')} ({me.get('account_type')})")
        elif args == ["me"]:
            me = req("GET", "/me", fields="user_id,username,account_type", access_token=refresh()["token"])
            print(f"✅ Токен работает: @{me.get('username')} ({me.get('account_type')})")
        elif args == ["refresh"]:
            refresh(force=True)
            print("✅ Токен продлён на 60 дней")
        else:
            print(__doc__)
    except IGError as e:
        sys.exit(f"❌ {e}")
