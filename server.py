#!/usr/bin/env python3
"""Interfaz web local de UATU.

  python3 server.py [--port 8787] [--no-open]

Por defecto sólo escucha en 127.0.0.1: las reviews salen con la cuenta de GitHub
de quien lo ejecuta. Con `--host 0.0.0.0` se abre a la red local, sin autenticación. Las revisiones se encolan y las ejecuta un único hilo, una detrás
de otra; publicar o descartar un borrador es inmediato.
"""
import argparse
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


# ----------------------------------------------------------------- HTTP

DRAFT_RE = re.compile(r"^/api/drafts/([A-Za-z0-9._-]+)/(\d+)(?:/(publish|discard))?$")
PR_RE = re.compile(r"^/api/prs/([A-Za-z0-9._-]+)/(\d+)/(conversation|approve)$")


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
            return self.handle_api(lambda: {
                "prs": overview(force), "jobs": jobs_snapshot(),
                "config": {"repos": uatu.CONFIG["repos"], "me": uatu.ME, "org": uatu.ORG,
                           "max_prs_per_run": uatu.CONFIG["max_prs_per_run"]},
                "generated_at": uatu.now(),
            })
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
        self.send_json({"error": "no encontrado"}, 404)

    def do_POST(self):
        path = self.path.partition("?")[0]
        if path == "/api/review":
            return self.handle_api(self.post_review)
        if path == "/api/review-all":
            return self.handle_api(self.post_review_all)
        m = PR_RE.match(path)
        if m and m.group(3) == "approve":
            return self.handle_api(lambda: self.post_approve(m.group(1), int(m.group(2))))
        m = DRAFT_RE.match(path)
        if m and m.group(3) == "publish":
            return self.handle_api(lambda: self.post_publish(m.group(1), int(m.group(2))))
        if m and m.group(3) == "discard":
            return self.handle_api(lambda: self.post_discard(m.group(1), int(m.group(2))))
        self.send_json({"error": "no encontrado"}, 404)

    def do_PUT(self):
        path = self.path.partition("?")[0]
        m = DRAFT_RE.match(path)
        if m and not m.group(3):
            return self.handle_api(lambda: self.put_draft(m.group(1), int(m.group(2))))
        self.send_json({"error": "no encontrado"}, 404)

    # --- handlers
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
        respuestas a mis comentarios (uatu.can_approve decide)."""
        if repo not in uatu.CONFIG["repos"]:
            raise ApiError(400, "repo inválido")
        body = self.read_body()
        try:
            res = uatu.approve_pr(repo, number, str(body.get("message") or ""))
        except ValueError as err:
            raise ApiError(409, str(err))
        invalidate()
        return res

    def post_discard(self, repo, number):
        draft = self.get_draft(repo, number)
        try:
            draft = uatu.discard_draft(draft)
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
