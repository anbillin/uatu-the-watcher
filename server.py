#!/usr/bin/env python3
"""Interfaz web local de UATU.

  python3 server.py [--port 8787] [--no-open]

Por defecto sólo escucha en 127.0.0.1: las reviews salen con la cuenta de GitHub
de quien lo ejecuta. Con `--host 0.0.0.0` se abre a la red local, sin autenticación. Las revisiones se encolan y las ejecuta un único hilo, una detrás
de otra; publicar o descartar un borrador es inmediato.
"""
import argparse
import collections
import json
import queue
import re
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uatu

WEB_DIR = Path(__file__).resolve().parent / "web"
OVERVIEW_TTL = 60  # segundos de caché de `gh pr list`

JOBS = {}            # key -> job
JOBS_LOCK = threading.Lock()
QUEUE = queue.Queue()
CACHE = {"rows": None, "at": 0.0, "error": None}
CACHE_LOCK = threading.Lock()
# Setup: comprobaciones cacheadas y la tarea larga en curso (importar o probar la voz), que va en
# su propio hilo para no esperar detrás de una revisión de 4 minutos.
SETUP = {"checks": None, "gh_user": None, "org_repos": None, "access": None, "job": None}
SETUP_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------- trabajos

def fresh_pending_draft(repo: str, number: int, head=None):
    """Borrador pendiente que sigue valiendo (mismo head si se conoce)."""
    draft = uatu.load_draft(repo, number)
    if not draft or draft["status"] != "pending":
        return None
    if head and draft["head"] != head:
        return None
    return draft


def enqueue(repo: str, number: int, full: bool) -> dict:
    key = uatu.state_key(repo, number)
    if not full:
        draft = fresh_pending_draft(repo, number)
        row = next((r for r in overview() if r["key"] == key), None)
        if draft and not (row and row["head"] != draft["head"]):
            raise ApiError(409, f"{key} ya tiene un borrador pendiente con "
                                f"{len(draft['comments'])} comentarios: ábrelo, publícalo o descártalo")
    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job["status"] in ("queued", "running"):
            return job
        job = {"key": key, "repo": repo, "number": number, "full": full, "status": "queued",
               "queued_at": uatu.now(), "started_at": None, "finished_at": None, "message": None}
        JOBS[key] = job
    QUEUE.put(job)
    return job


def worker() -> None:
    while True:
        job = QUEUE.get()
        job["status"], job["started_at"] = "running", uatu.now()
        try:
            pr = uatu.get_pr(job["repo"], job["number"])
            if not job["full"] and fresh_pending_draft(job["repo"], job["number"], pr["headRefOid"]):
                raise uatu.Skip("ya hay un borrador pendiente para este commit; ábrelo o descártalo")
            draft = uatu.make_draft(pr, force_full=job["full"])
            job["status"] = "done"
            job["message"] = f"{len(draft['comments'])} comentarios en el borrador"
        except uatu.Skip as skip:
            job["status"], job["message"] = "skipped", str(skip)
            uatu.log(f"{job['key']}: se salta, {skip}")
        except Exception as exc:  # noqa: BLE001
            job["status"], job["message"] = "error", str(exc)[:600]
            uatu.log(f"{job['key']}: ERROR {exc}")
            traceback.print_exc()
        finally:
            job["finished_at"] = uatu.now()
            invalidate()


def jobs_snapshot() -> dict:
    with JOBS_LOCK:
        return {k: dict(v) for k, v in JOBS.items()}


# ----------------------------------------------------------------- setup

def setup_job(kind: str, fn) -> dict:
    with SETUP_LOCK:
        job = SETUP["job"]
        if job and job["status"] == "running":
            raise ApiError(409, f"ya hay una tarea de setup en marcha ({job['kind']}); espera a que acabe")
        job = {"kind": kind, "status": "running", "started_at": uatu.now(), "finished_at": None,
               "result": None, "error": None}
        SETUP["job"] = job

    def run():
        try:
            job["result"] = fn()
            job["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            job["status"], job["error"] = "error", str(exc)[:1000]
            uatu.log(f"setup ({kind}): ERROR {exc}")
            traceback.print_exc()
        finally:
            job["finished_at"] = uatu.now()

    threading.Thread(target=run, name=f"uatu-setup-{kind}", daemon=True).start()
    return job


def refresh_checks(repos=None) -> None:
    """Comprobaciones del entorno; con `repos`, también el acceso SSH a cada uno (1-2 s por repo)."""
    with SETUP_LOCK:
        SETUP["checks"] = uatu.run_checks()
        SETUP["gh_user"] = uatu.gh_user() if any(c["name"] == "gh" and c["ok"] for c in SETUP["checks"]) else None
        try:
            SETUP["org_repos"] = uatu.list_org_repos() if SETUP["gh_user"] else []
        except Exception as exc:  # noqa: BLE001 — sin la lista se eligen a mano
            uatu.log(f"setup: no se pudo listar los repos de {uatu.ORG}: {str(exc)[:200]}")
            SETUP["org_repos"] = []
        if repos is not None:
            SETUP["access"] = [uatu.check_repo_access(r) for r in repos]


def setup_state() -> dict:
    if SETUP["checks"] is None:
        refresh_checks()
    with SETUP_LOCK:
        return {**uatu.setup_snapshot(), **{k: v for k, v in SETUP.items()}}


# ----------------------------------------------------------------- caché de PRs

def invalidate() -> None:
    with CACHE_LOCK:
        CACHE["at"] = 0.0


def overview(force: bool = False) -> list:
    with CACHE_LOCK:
        fresh = CACHE["rows"] is not None and time.time() - CACHE["at"] < OVERVIEW_TTL
        if fresh and not force:
            return CACHE["rows"]
        rows = uatu.overview()
        CACHE.update(rows=rows, at=time.time())
        return rows


# ----------------------------------------------------------------- diff de la PR

DIFFS = collections.OrderedDict()   # (repo, number, head) -> diff crudo; un commit no cambia
DIFFS_LOCK = threading.Lock()
DIFFS_MAX = 20


def pr_diff(repo: str, number: int, base_ref: str, head: str) -> str:
    key = (repo, number, head)
    with DIFFS_LOCK:
        if key in DIFFS:
            DIFFS.move_to_end(key)
            return DIFFS[key]
    diff = uatu.pr_diff(repo, base_ref, head)
    with DIFFS_LOCK:
        DIFFS[key] = diff
        while len(DIFFS) > DIFFS_MAX:
            DIFFS.popitem(last=False)
    return diff


# ----------------------------------------------------------------- HTTP

DRAFT_RE = re.compile(r"^/api/drafts/([A-Za-z0-9._-]+)/(\d+)(?:/(publish|discard|create|comments)(?:/(\d+))?)?$")
PR_RE = re.compile(r"^/api/prs/([A-Za-z0-9._-]+)/(\d+)/(conversation|approve|react|diff)$")
SETUP_RE = re.compile(r"^/api/setup(?:/(checks|voice/import|voice/preview))?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "uatu/1"

    def log_message(self, fmt, *args):  # silencia el access log
        pass

    # --- respuestas
    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path, ctype: str):
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ApiError(400, "JSON inválido")

    def handle_api(self, fn):
        try:
            self.send_json(fn())
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": str(exc)[:1000]}, 500)

    # --- rutas
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/prs":
            force = "refresh=1" in query
            return self.handle_api(lambda: self.get_prs(force))
        if path == "/api/setup":
            return self.handle_api(setup_state)
        if path == "/api/jobs":
            return self.handle_api(lambda: {"jobs": jobs_snapshot()})
        if path == "/api/log":
            return self.handle_api(lambda: {"lines": list(uatu.LOG_BUFFER)[-80:]})
        m = DRAFT_RE.match(path)
        if m and not m.group(3):
            return self.handle_api(lambda: self.get_draft(m.group(1), int(m.group(2))))
        m = PR_RE.match(path)
        if m and m.group(3) == "conversation":
            return self.handle_api(lambda: self.get_conversation(m.group(1), int(m.group(2))))
        if m and m.group(3) == "diff":
            head = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("head")
            return self.handle_api(lambda: self.get_diff(m.group(1), int(m.group(2)), head))
        self.send_json({"error": "no encontrado"}, 404)

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path == "/api/review":
            return self.handle_api(self.post_review)
        if path == "/api/review-all":
            return self.handle_api(self.post_review_all)
        m = SETUP_RE.match(path)
        if m and m.group(1) == "checks":
            return self.handle_api(self.post_setup_checks)
        if m and m.group(1) == "voice/import":
            return self.handle_api(self.post_voice_import)
        if m and m.group(1) == "voice/preview":
            return self.handle_api(self.post_voice_preview)
        m = PR_RE.match(path)
        if m and m.group(3) == "approve":
            return self.handle_api(lambda: self.post_approve(m.group(1), int(m.group(2))))
        if m and m.group(3) == "react":
            return self.handle_api(lambda: self.post_react(m.group(1)))
        m = DRAFT_RE.match(path)
        if m and m.group(3) == "publish":
            return self.handle_api(lambda: self.post_publish(m.group(1), int(m.group(2))))
        if m and m.group(3) == "discard":
            return self.handle_api(lambda: self.post_discard(m.group(1), int(m.group(2))))
        if m and m.group(3) == "create":
            return self.handle_api(lambda: self.post_create(m.group(1), int(m.group(2))))
        if m and m.group(3) == "comments" and not m.group(4):
            return self.handle_api(lambda: self.post_comment(m.group(1), int(m.group(2))))
        self.send_json({"error": "no encontrado"}, 404)

    def do_DELETE(self):
        path = self.path.partition("?")[0]
        m = DRAFT_RE.match(path)
        if m and m.group(3) == "comments" and m.group(4):
            return self.handle_api(lambda: self.delete_comment(m.group(1), int(m.group(2)), int(m.group(4))))
        self.send_json({"error": "no encontrado"}, 404)

    def do_PUT(self):
        path = self.path.partition("?")[0]
        if path == "/api/setup":
            return self.handle_api(self.put_setup)
        m = DRAFT_RE.match(path)
        if m and not m.group(3):
            return self.handle_api(lambda: self.put_draft(m.group(1), int(m.group(2))))
        self.send_json({"error": "no encontrado"}, 404)

    # --- handlers
    def get_prs(self, force):
        """La lista. Sin perfil completo no se llama a GitHub: la web manda a Setup."""
        config = {"repos": uatu.CONFIG.get("repos") or [], "me": uatu.ME, "org": uatu.ORG,
                  "name": uatu.CONFIG.get("name"), "max_prs_per_run": uatu.CONFIG["max_prs_per_run"]}
        if not uatu.is_ready():
            return {"ready": False, "prs": [], "jobs": {}, "config": config, "generated_at": uatu.now()}
        return {"ready": True, "prs": overview(force), "jobs": jobs_snapshot(), "config": config,
                "generated_at": uatu.now()}

    def put_setup(self):
        """Guarda perfil y voz; la config se recarga en caliente."""
        body = self.read_body()
        try:
            profile = uatu.clean_profile(body.get("profile") or {})
        except ValueError as err:
            raise ApiError(400, str(err))
        voice = body.get("voice")
        uatu.save_profile(profile, str(voice) if voice is not None else None)
        invalidate()
        return setup_state()

    def post_setup_checks(self):
        body = self.read_body()
        repos = body.get("repos")
        refresh_checks([str(r) for r in repos] if isinstance(repos, list) else (uatu.CONFIG.get("repos") or []))
        return setup_state()

    def post_voice_import(self):
        """Destila la voz del historial de GitHub en segundo plano; el resultado va al textarea,
        no a disco: se guarda con el resto del perfil."""
        body = self.read_body()
        me = str(body.get("me") or uatu.ME or "").strip()
        if not me:
            raise ApiError(400, "hace falta el usuario de GitHub")
        repos = [str(r) for r in body.get("repos") or []] or (uatu.CONFIG.get("repos") or [])
        if not repos:
            raise ApiError(400, "elige al menos un repo")
        name = str(body.get("name") or "").strip() or None
        return {"job": setup_job("import", lambda: uatu.import_voice(me, name, repos))}

    def post_voice_preview(self):
        body = self.read_body()
        voice = str(body.get("voice") or "").strip()
        if not voice:
            raise ApiError(400, "la voz está vacía")
        name = str(body.get("name") or "").strip() or None
        return {"job": setup_job("preview", lambda: uatu.preview_voice(voice, name))}

    def get_draft(self, repo, number):
        draft = uatu.load_draft(repo, number)
        if not draft:
            raise ApiError(404, "no hay borrador para esa PR")
        return draft

    def post_review(self):
        body = self.read_body()
        repo, number = body.get("repo"), body.get("number")
        if repo not in uatu.CONFIG["repos"] or not isinstance(number, int):
            raise ApiError(400, "repo o número de PR inválidos")
        return {"job": enqueue(repo, number, bool(body.get("full")))}

    def post_review_all(self):
        rows = uatu.pending_rows(overview(force=True))
        jobs = []
        for r in rows[: uatu.CONFIG["max_prs_per_run"]]:
            try:
                jobs.append(enqueue(r["repo"], r["number"], False))
            except ApiError:
                pass
        return {"jobs": jobs, "pending": len(rows)}

    def put_draft(self, repo, number):
        """Guarda las ediciones: resumen, cuerpo y activación de cada comentario.
        Los comentarios se identifican por posición; ruta y líneas no se tocan."""
        draft = self.get_draft(repo, number)
        if draft["status"] != "pending":
            raise ApiError(409, f"el borrador ya está {draft['status']}")
        body = self.read_body()
        if "summary" in body:
            draft["summary"] = str(body["summary"]).strip()
        if "praise" in body and draft.get("praise"):
            draft["praise"]["body"] = str(body["praise"].get("body", draft["praise"]["body"]))
            draft["praise"]["enabled"] = bool(body["praise"].get("enabled", draft["praise"]["enabled"]))
        edits = body.get("comments") or []
        if len(edits) != len(draft["comments"]):
            raise ApiError(409, "el borrador cambió; recarga la página")
        for c, e in zip(draft["comments"], edits):
            c["body"] = str(e.get("body", c["body"]))
            c["enabled"] = bool(e.get("enabled", True))
        uatu.save_draft(draft)
        invalidate()
        return draft

    def post_publish(self, repo, number):
        draft = self.get_draft(repo, number)
        try:
            draft = uatu.publish_draft(draft)
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return draft

    def get_conversation(self, repo, number):
        """Mis hilos en la PR con sus respuestas (en vivo, sin caché); marca las respuestas
        como vistas, así que la lista deja de contarlas como nuevas."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        conv = uatu.get_conversation(repo, number)
        invalidate()
        return conv

    def post_approve(self, repo, number):
        """Review APPROVE sobre el commit actual. Sólo si UATU ya revisó ese commit o hay
        respuestas a mis comentarios (uatu.can_approve decide). Con `discard_draft`, un borrador
        pendiente para ese commit no bloquea: se aprueba sin publicarlo y se descarta."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        body = self.read_body()
        try:
            res = uatu.approve_pr(repo, number, str(body.get("message") or ""), bool(body.get("discard_draft")))
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return res

    def post_react(self, repo):
        """Pone (on=true) o quita mi reacción en un comentario de la conversación. Devuelve
        las reacciones del comentario tal y como quedan, para pintarlas sin recargar."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        body = self.read_body()
        node_id, content = str(body.get("comment") or ""), str(body.get("content") or "")
        if not node_id or content not in uatu.REACTIONS:
            raise ApiError(400, "hace falta el comentario y una reacción válida")
        return {"reactions": uatu.react(node_id, content, bool(body.get("on", True)))}

    def post_discard(self, repo, number):
        draft = self.get_draft(repo, number)
        try:
            draft = uatu.discard_draft(draft)
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return draft

    # --- código completo y comentarios propios
    def get_diff(self, repo, number, head):
        """El diff de la PR por ficheros, anclado a `head` (por defecto el commit actual de la PR).
        Con un borrador abierto la web pide el head del borrador, que es donde se anclan los comentarios."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        if head and not re.fullmatch(r"[0-9a-f]{7,40}", head):
            raise ApiError(400, "head inválido")
        # La base se lee de la PR en vivo, no del borrador: si la rama base se borró (hotfix ya
        # mergeado), GitHub reapunta la PR y el compare con la rama vieja da 404.
        pr = uatu.get_pr(repo, number)
        head, base_ref = head or pr["headRefOid"], pr["baseRefName"]
        files = uatu.parse_diff(pr_diff(repo, number, base_ref, head))
        return {"repo": repo, "number": number, "head": head, "base_ref": base_ref, "files": files,
                "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}

    def post_create(self, repo, number):
        """Borrador manual: vacío y sin Claude, para comentar el código a mano."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        key = uatu.state_key(repo, number)
        with JOBS_LOCK:
            job = JOBS.get(key)
            if job and job["status"] in ("queued", "running"):
                raise ApiError(409, f"{key} se está revisando ahora mismo; espera al borrador")
        if fresh_pending_draft(repo, number):
            raise ApiError(409, f"{key} ya tiene un borrador pendiente: ábrelo")
        draft = uatu.new_manual_draft(uatu.get_pr(repo, number))
        invalidate()
        return draft

    def post_comment(self, repo, number):
        draft = self.get_draft(repo, number)
        body = self.read_body()
        try:
            path, line = str(body.get("path") or ""), int(body.get("line") or 0)
            start = int(body.get("start_line") or line)
        except (TypeError, ValueError):
            raise ApiError(400, "path y line son obligatorios")
        if not path or line < 1:
            raise ApiError(400, "path y line son obligatorios")
        diff = pr_diff(repo, number, draft["base_ref"], draft["head"])
        try:
            draft = uatu.add_own_comment(draft, diff, path, start, line, str(body.get("body") or ""))
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return draft

    def delete_comment(self, repo, number, index):
        draft = self.get_draft(repo, number)
        try:
            draft = uatu.remove_own_comment(draft, index)
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return draft


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 para acceder desde otros equipos de la red (sin autenticación)")
    ap.add_argument("--no-open", action="store_true", help="no abre el navegador")
    args = ap.parse_args()
    threading.Thread(target=worker, name="uatu-worker", daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    uatu.log(f"web UATU en {url} escuchando en {args.host}:{args.port} (Ctrl-C para parar)")
    if args.host != "127.0.0.1":
        uatu.log("AVISO: accesible desde la red local sin autenticación; cualquiera que llegue puede publicar reviews con tu cuenta")
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        uatu.log("web UATU parada")


if __name__ == "__main__":
    main()
