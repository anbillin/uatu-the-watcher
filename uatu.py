#!/usr/bin/env python3
"""UATU: revisa las PRs abiertas de los compañeros con `claude` y deja un
BORRADOR de review por PR. Nada se publica en GitHub hasta que alguien lo
ve y pulsa «Publicar» (web: `server.py`, o `uatu.py publish`).

Uso:
  server.py [--port 8787]                   # interfaz web (lo habitual)
  uatu.py status                            # PRs candidatas y su estado
  uatu.py review <repo> <pr> [--full] [--publish]
  uatu.py run [--repo gofre] [--max N]      # borradores para las pendientes
  uatu.py publish <repo> <pr>               # publica un borrador existente
  uatu.py replies <repo> <pr>               # respuestas a mis comentarios
  uatu.py approve <repo> <pr> [-m msg]      # APPROVE, sólo si ya está revisada al día
"""
import argparse
import collections
import datetime as dt
import fcntl
import json
import random
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
REPOS_DIR = ROOT / "repos"
STATE_DIR = ROOT / "state"
WORK_DIR = STATE_DIR / "work"
DRAFTS_DIR = STATE_DIR / "drafts"
LOGS_DIR = ROOT / "logs"
STATE_FILE = STATE_DIR / "reviewed.json"
PRAISE_FILE = STATE_DIR / "praise.json"
SEEN_FILE = STATE_DIR / "seen.json"   # última respuesta vista por PR, para marcar las nuevas
PROMPT_FILE = ROOT / "prompts" / "review-pr.md"
AGREEMENTS_FILE = ROOT / "prompts" / "code-agreements.json"
# Ids de los Code Agreements, para que el esquema de salida sólo admita los que existen.
AGREEMENT_IDS = [a["id"] for a in json.loads(AGREEMENTS_FILE.read_text())["agreements"]]

MARKER_RE = re.compile(r"<!-- uatu sha=([0-9a-f]{7,40}) -->")
ORG = CONFIG["github_org"]
ME = CONFIG["me"]
SEVERITY_ORDER = {"must": 0, "should": 1, "nit": 2}

# Últimas líneas de log, para enseñarlas en la web.
LOG_BUFFER = collections.deque(maxlen=400)
STATE_LOCK = threading.RLock()

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "praise": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["must", "should", "nit"]},
                    "rule": {"type": "string"},
                    "agreement": {"type": "string", "enum": [""] + AGREEMENT_IDS},
                    "body": {"type": "string"},
                },
                "required": ["path", "start_line", "line", "severity", "rule", "agreement", "body"],
            },
        },
    },
    "required": ["summary", "praise", "findings"],
}


class Skip(Exception):
    """La PR no se revisa esta vez; el mensaje dice por qué."""


# ----------------------------------------------------------------- utilidades

def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    LOG_BUFFER.append(line)
    LOGS_DIR.mkdir(exist_ok=True)
    with open(LOGS_DIR / "uatu.log", "a") as fh:
        fh.write(line + "\n")


def sh(cmd, cwd=None, check=True, input_text=None) -> str:
    res = subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, input=input_text
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr.strip()}"
        )
    return res.stdout


def gh_json(args, repo=None):
    cmd = ["gh"] + args
    if repo:
        cmd += ["-R", f"{ORG}/{repo}"]
    return json.loads(sh(cmd) or "null")


def load_state() -> dict:
    with STATE_LOCK:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {}


def update_state(key: str, entry: dict) -> None:
    with STATE_LOCK:
        state = load_state()
        state[key] = entry
        STATE_DIR.mkdir(exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def state_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


# ----------------------------------------------------------------- borradores

def draft_path(repo: str, number: int) -> Path:
    return DRAFTS_DIR / f"{repo}-{number}.json"


def load_draft(repo: str, number: int):
    p = draft_path(repo, number)
    return json.loads(p.read_text()) if p.exists() else None


def save_draft(draft: dict) -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft_path(draft["repo"], draft["number"]).write_text(
        json.dumps(draft, indent=2, ensure_ascii=False))


def load_praise_log() -> dict:
    with STATE_LOCK:
        return json.loads(PRAISE_FILE.read_text()) if PRAISE_FILE.exists() else {}


def record_praise(author: str) -> None:
    with STATE_LOCK:
        logd = load_praise_log()
        logd[author] = now()
        STATE_DIR.mkdir(exist_ok=True)
        PRAISE_FILE.write_text(json.dumps(logd, indent=2, ensure_ascii=False, sort_keys=True))


def praise_gate(author: str) -> str:
    """Decide si el elogio propuesto entra por defecto. Devuelve 'ok', 'cooldown' o 'rate'.
    El usuario siempre puede marcarlo a mano en el borrador."""
    cfg = CONFIG.get("praise") or {}
    last = load_praise_log().get(author)
    if last:
        days = (dt.datetime.now() - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).days
        if days < cfg.get("cooldown_days", 5):
            return "cooldown"
    if random.random() > cfg.get("rate", 0.35):
        return "rate"
    return "ok"


def praise_text(draft: dict) -> str:
    pr = draft.get("praise") or {}
    return (pr.get("body") or "").strip() if pr.get("enabled") else ""


def enabled_comments(draft: dict) -> list:
    return [c for c in draft["comments"] if c.get("enabled", True) and c["body"].strip()]


def draft_summary(draft: dict, head: str) -> dict:
    """Lo que la lista de PRs necesita saber de un borrador."""
    return {
        "status": draft["status"],
        "comments": len(enabled_comments(draft)),
        "total": len(draft["comments"]),
        "created_at": draft["created_at"],
        "published_at": draft.get("published_at"),
        "mode": draft["mode"],
        "stale": draft["head"] != head,
        "cost_usd": draft.get("claude", {}).get("cost_usd"),
        "praise": bool(praise_text(draft)),
    }


# ----------------------------------------------------------------- GitHub

PR_FIELDS = "number,title,body,author,isDraft,headRefName,headRefOid,baseRefName,url,updatedAt,additions,deletions"


def list_candidates(repo: str) -> list:
    prs = gh_json(
        ["pr", "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS], repo
    )
    excluded = set(CONFIG.get("exclude_authors", [])) | {ME}
    out = []
    for pr in prs:
        if pr["isDraft"]:
            continue
        if pr["author"]["login"] in excluded:
            continue
        pr["repo"] = repo
        out.append(pr)
    out.sort(key=lambda p: p["updatedAt"], reverse=True)
    return out


def get_pr(repo: str, number: int) -> dict:
    pr = gh_json(["pr", "view", str(number), "--json", PR_FIELDS], repo)
    pr["repo"] = repo
    return pr


def last_reviewed_sha(repo: str, number: int, state: dict):
    """SHA de la última revisión nuestra: primero el estado local, si no, el
    marcador oculto en las reviews publicadas en GitHub."""
    entry = state.get(state_key(repo, number))
    if entry:
        return entry["sha"]
    bodies = gh_json(
        ["api", f"repos/{ORG}/{repo}/pulls/{number}/reviews", "--paginate",
         "--jq", f'[.[] | select(.user.login=="{ME}") | .body]'],
    ) or []
    shas = [m.group(1) for b in bodies for m in [MARKER_RE.search(b or "")] if m]
    return shas[-1] if shas else None


def overview() -> list:
    """Todas las PRs candidatas con su estado local, borrador incluido."""
    state, seen = load_state(), load_seen()
    rows = []
    for repo in CONFIG["repos"]:
        prs = list_candidates(repo)
        try:
            convs = fetch_conversations(repo, [p["number"] for p in prs])
        except Exception as exc:  # noqa: BLE001 — la lista sigue valiendo sin esta capa
            log(f"{repo}: no se pudo leer aprobaciones/respuestas: {str(exc)[:200]}")
            convs = {}
        for pr in prs:
            key = state_key(repo, pr["number"])
            entry = state.get(key)
            conv = convs.get(pr["number"])
            if entry and entry["sha"] == pr["headRefOid"]:
                status = "al-dia"
            elif entry:
                status = "nuevos-commits"
            else:
                status = "pendiente"
            draft = load_draft(repo, pr["number"])
            rows.append({
                "key": key, "repo": repo, "number": pr["number"], "title": pr["title"],
                "author": pr["author"]["login"], "url": pr["url"], "head": pr["headRefOid"],
                "head_ref": pr["headRefName"], "base_ref": pr["baseRefName"],
                "updated_at": pr["updatedAt"], "additions": pr["additions"],
                "deletions": pr["deletions"], "status": status, "last": entry,
                "draft": draft_summary(draft, pr["headRefOid"]) if draft else None,
                "approval": conv["approval"] if conv else None,
                "conversation": conversation_summary(conv, key, seen) if conv else None,
                "can_approve": can_approve(pr, entry, conv["approval"])[0] if conv else False,
            })
    return rows


# ----------------------------------------------------------------- conversación y aprobación

CONVERSATION_FRAGMENT = """
fragment PR on PullRequest {
  number headRefOid
  reviews(last: 30, author: "%s") { nodes { state submittedAt url commit { oid } } }
  reviewThreads(first: 100) { nodes { id isResolved isOutdated path line originalLine
    comments(first: 30) { nodes { databaseId author { login } body createdAt url } } } }
}"""


def load_seen() -> dict:
    with STATE_LOCK:
        return json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}


def mark_seen(key: str, latest: str) -> None:
    """Anota hasta qué respuesta (createdAt ISO) se ha visto ya en esa PR."""
    if not latest:
        return
    with STATE_LOCK:
        seen = load_seen()
        if seen.get(key, "") >= latest:
            return
        seen[key] = latest
        STATE_DIR.mkdir(exist_ok=True)
        SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def my_approval(reviews: list):
    """Mi última review con opinión (approve / request-changes / dismissed). Sólo cuenta
    si es un APPROVED vigente: un COMMENT posterior no la quita, un dismiss sí."""
    opinionated = [r for r in reviews if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    last = opinionated[-1] if opinionated else None
    if not last or last["state"] != "APPROVED":
        return None
    return {"at": last["submittedAt"], "sha": (last.get("commit") or {}).get("oid"), "url": last["url"]}


def parse_threads(nodes: list) -> list:
    """Los hilos que empiezan con un comentario mío, con sus respuestas."""
    threads = []
    for t in nodes:
        cs = t["comments"]["nodes"]
        if not cs or (cs[0].get("author") or {}).get("login") != ME:
            continue
        def item(c):
            return {"id": c["databaseId"], "author": (c.get("author") or {}).get("login") or "(borrado)",
                    "body": c["body"], "at": c["createdAt"], "url": c["url"]}
        replies = [item(c) for c in cs[1:]]
        threads.append({
            "id": t["id"], "path": t["path"], "line": t["line"] or t["originalLine"],
            "resolved": t["isResolved"], "outdated": t["isOutdated"],
            "comment": item(cs[0]), "replies": replies,
            # alguien ha contestado y la última palabra no es mía: me toca mirar
            "awaiting_me": bool(replies) and replies[-1]["author"] != ME and not t["isResolved"],
        })
    return threads


def conversation_summary(conv: dict, key: str, seen: dict) -> dict:
    """Lo que la lista necesita: cuántas respuestas hay, cuántas sin ver, cuántas esperan."""
    others = [r for t in conv["threads"] for r in t["replies"] if r["author"] != ME]
    last_seen = seen.get(key, "")
    return {
        "threads": len(conv["threads"]),
        "replies": len(others),
        "new": sum(1 for r in others if r["at"] > last_seen),
        "awaiting_me": sum(1 for t in conv["threads"] if t["awaiting_me"]),
        "unresolved": sum(1 for t in conv["threads"] if not t["resolved"]),
        "last_reply_at": max((r["at"] for r in others), default=None),
    }


def fetch_conversations(repo: str, numbers: list) -> dict:
    """Una sola consulta GraphQL por repo: mi aprobación y los hilos de mis comentarios
    en cada PR. Devuelve {número: {"approval", "threads", "head"}}."""
    if not numbers:
        return {}
    body = "\n".join(f"p{n}: pullRequest(number: {n}) {{ ...PR }}" for n in numbers)
    query = ("query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {\n"
             f"{body}\n}} }}" + CONVERSATION_FRAGMENT % ME)
    data = gh_json(["api", "graphql", "-f", f"query={query}", "-F", f"owner={ORG}", "-F", f"name={repo}"])
    out = {}
    for pr in ((data.get("data") or {}).get("repository") or {}).values():
        if not pr:
            continue
        out[pr["number"]] = {
            "head": pr["headRefOid"],
            "approval": my_approval(pr["reviews"]["nodes"]),
            "threads": parse_threads(pr["reviewThreads"]["nodes"]),
        }
    return out


def get_conversation(repo: str, number: int) -> dict:
    """Conversación completa de una PR, para verla en la web; marca las respuestas como vistas."""
    pr = get_pr(repo, number)
    conv = fetch_conversations(repo, [number]).get(number) or {"approval": None, "threads": []}
    key = state_key(repo, number)
    entry = load_state().get(key)
    can, why = can_approve(pr, entry, conv["approval"])
    seen_until = load_seen().get(key)
    summary = conversation_summary(conv, key, load_seen())
    mark_seen(key, summary["last_reply_at"])
    return {
        "key": key, "repo": repo, "number": number, "title": pr["title"], "author": pr["author"]["login"],
        "url": pr["url"], "head": pr["headRefOid"], "head_ref": pr["headRefName"], "base_ref": pr["baseRefName"],
        "last": entry, "approval": conv["approval"], "threads": conv["threads"],
        "summary": summary, "seen_until": seen_until,  # respuestas posteriores: las «nuevas» de esta visita
        "can_approve": can, "approve_blocked": why,
    }


def can_approve(pr: dict, entry, approval):
    """Sólo se aprueba lo que ya se ha revisado hasta su commit actual y no está aprobado."""
    if approval:
        return False, "ya está aprobada"
    if not entry:
        return False, "UATU no ha revisado esta PR"
    if entry["sha"] != pr["headRefOid"]:
        return False, "hay commits sin revisar: revisa el delta antes de aprobar"
    draft = load_draft(pr["repo"], pr["number"])
    if draft and draft["status"] == "pending" and draft["head"] == pr["headRefOid"]:
        return False, "hay un borrador pendiente para este commit: publícalo o descártalo"
    return True, None


def approve_pr(repo: str, number: int, message: str = "") -> dict:
    """Manda una review APPROVE sobre el commit actual. Es la única review que no es COMMENT,
    y siempre la pide una persona desde la web o la CLI."""
    pr = get_pr(repo, number)
    key = state_key(repo, number)
    conv = fetch_conversations(repo, [number]).get(number) or {"approval": None}
    ok, why = can_approve(pr, load_state().get(key), conv["approval"])
    if not ok:
        raise ValueError(why)
    payload = {"commit_id": pr["headRefOid"], "event": "APPROVE", "body": (message or "").strip()}
    sh(["gh", "api", "-X", "POST", f"repos/{ORG}/{repo}/pulls/{number}/reviews", "--input", "-"],
       input_text=json.dumps(payload))
    log(f"{key}: aprobada en {pr['headRefOid'][:10]}{' con mensaje' if payload['body'] else ''} · {pr['url']}")
    return {"key": key, "sha": pr["headRefOid"], "at": now()}


# ----------------------------------------------------------------- git

def ensure_clone(repo: str) -> Path:
    path = REPOS_DIR / repo
    if not path.exists():
        REPOS_DIR.mkdir(exist_ok=True)
        log(f"clonando {ORG}/{repo} en {path}")
        sh(["git", "clone", "--quiet", f"git@github.com:{ORG}/{repo}.git", str(path)])
    return path


def prepare_checkout(path: Path, pr: dict) -> str:
    """Trae base y head, deja el repo en el head (detached) y devuelve el merge-base."""
    number = pr["number"]
    sh(["git", "fetch", "--quiet", "--force", "origin",
        f"pull/{number}/head:refs/uatu/pr-{number}",
        f"+refs/heads/{pr['baseRefName']}:refs/remotes/origin/{pr['baseRefName']}"], cwd=path)
    sh(["git", "reset", "--quiet", "--hard"], cwd=path)
    sh(["git", "clean", "--quiet", "-fdx", "-e", ".claude/settings.local.json"], cwd=path)
    sh(["git", "checkout", "--quiet", "--detach", pr["headRefOid"]], cwd=path)
    return sh(["git", "merge-base", f"origin/{pr['baseRefName']}", pr["headRefOid"]],
              cwd=path).strip()


def is_ancestor(path: Path, older: str, newer: str) -> bool:
    res = subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                         cwd=path, capture_output=True)
    return res.returncode == 0


def git_diff(path: Path, a: str, b: str) -> str:
    return sh(["git", "diff", "--no-color", "--no-ext-diff", "-U3", f"{a}..{b}"], cwd=path)


def count_changed_lines(diff: str) -> int:
    return sum(1 for ln in diff.splitlines()
               if (ln.startswith("+") or ln.startswith("-"))
               and not ln.startswith("+++") and not ln.startswith("---"))


def commentable_lines(diff: str) -> dict:
    """Para cada fichero del diff, el conjunto de líneas del lado NUEVO en las
    que GitHub admite un comentario (añadidas y de contexto)."""
    files, current, new_ln = {}, None, 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            target = ln[4:].strip()
            current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if current is not None:
                files.setdefault(current, set())
            continue
        if ln.startswith("--- ") or ln.startswith("diff --git") or current is None:
            continue
        m = hunk_re.match(ln)
        if m:
            new_ln = int(m.group(1))
            continue
        if ln.startswith("\\"):
            continue
        if ln.startswith("+") or ln.startswith(" ") or ln == "":
            files[current].add(new_ln)
            new_ln += 1
        # las líneas '-' no avanzan el contador del lado nuevo
    return files


def code_context(repo_path: Path, path: str, start: int, end: int, around: int = 3) -> list:
    """Líneas del fichero alrededor del rango comentado, para verlas en la web
    sin depender de en qué commit esté el clon después."""
    try:
        lines = (repo_path / path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    a, b = max(1, start - around), min(len(lines), end + around)
    return [{"n": n, "text": lines[n - 1], "hit": start <= n <= end} for n in range(a, b + 1)]


# ----------------------------------------------------------------- code agreements

def load_agreements() -> dict:
    """Acuerdos del equipo (prompts/code-agreements.json), por id."""
    data = json.loads(AGREEMENTS_FILE.read_text())
    return {a["id"]: a for a in data["agreements"]}


def agreements_for(repo: str) -> list:
    """Los acuerdos que aplican a un repo: los generales más los de su ámbito (front/back)."""
    scope = (CONFIG.get("code_agreements") or {}).get("scope", {}).get(repo)
    return [a for a in load_agreements().values() if a["scope"] in (None, scope)]


def agreement_url(agreement: dict) -> str:
    base = (CONFIG.get("code_agreements") or {}).get("url", "")
    return f"{base}#{agreement['anchor']}" if agreement.get("anchor") else base


def render_agreements(repo: str) -> str:
    """Sección del prompt con los acuerdos aplicables al repo, con su id para citarlos."""
    lines = []
    for a in agreements_for(repo):
        lines.append(f"- `{a['id']}` · **{a['title']}**: {a['rule']}")
    return "\n".join(lines)


def attach_agreement(finding: dict) -> dict:
    """Si el hallazgo cita un acuerdo, deja el enlace a Notion al final del comentario
    (como hace el equipo) y la atribución en `rule`. El enlace lo pone UATU, no claude:
    así el anclaje es siempre el bueno."""
    aid = (finding.get("agreement") or "").strip()
    agreement = load_agreements().get(aid) if aid else None
    if aid and not agreement:
        log(f"  acuerdo desconocido '{aid}' en {finding['path']}:{finding['line']}, se publica sin enlace")
    if not agreement:
        return finding
    base = (CONFIG.get("code_agreements") or {}).get("url", "")
    body = finding["body"].rstrip()
    if base and base not in body:
        body = f"{body}\n{agreement_url(agreement)}"
    return {**finding, "body": body, "rule": f"Code Agreements › {agreement['title']}"}


# ----------------------------------------------------------------- claude

def render_prompt(pr: dict, head: str, diff_file: Path, delta_file: Path, mode: str, last_sha):
    if mode == "full":
        mode_expl = "Es la PR completa: primera revisión."
    else:
        mode_expl = (f"Son sólo los commits desde la última revisión (`{last_sha[:10]}..{head[:10]}`). "
                     "Lo anterior ya se comentó: no lo repitas, aunque sigas viéndolo en el diff completo.")
    body = (pr.get("body") or "").strip() or "(sin descripción)"
    repl = {
        "{{REPO}}": pr["repo"], "{{PR_NUMBER}}": str(pr["number"]), "{{PR_TITLE}}": pr["title"],
        "{{AUTHOR}}": pr["author"]["login"], "{{HEAD_REF}}": pr["headRefName"],
        "{{BASE_REF}}": pr["baseRefName"], "{{HEAD_SHA}}": head, "{{PR_BODY}}": body,
        "{{DIFF_FILE}}": str(diff_file), "{{DELTA_FILE}}": str(delta_file),
        "{{MODE_EXPLANATION}}": mode_expl, "{{MAX_FINDINGS}}": str(CONFIG["max_findings"]),
        "{{CODE_AGREEMENTS}}": render_agreements(pr["repo"]),
        "{{CODE_AGREEMENTS_URL}}": (CONFIG.get("code_agreements") or {}).get("url", ""),
    }
    text = PROMPT_FILE.read_text()
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def run_claude(repo_path: Path, prompt: str, pr: dict):
    """Devuelve (salida estructurada, metadatos de la ejecución)."""
    ccfg = CONFIG["claude"]
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(FINDINGS_SCHEMA),
        "--permission-mode", "dontAsk",
        "--allowedTools", "Read", "Grep", "Glob",
        "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
        "Bash(ls:*)", "Bash(cat:*)", "Bash(wc:*)",
        "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Agent",
        "--add-dir", str(WORK_DIR),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns", str(ccfg["max_turns"]),
        "--max-budget-usd", str(ccfg["max_budget_usd"]),
    ]
    if ccfg.get("model"):
        cmd += ["--model", ccfg["model"]]
    log(f"  claude revisando {pr['repo']}#{pr['number']} ...")
    res = subprocess.run(cmd, cwd=repo_path, text=True, capture_output=True)
    raw_path = WORK_DIR / f"{pr['repo']}-{pr['number']}-claude.json"
    raw_path.write_text(res.stdout)
    if res.returncode != 0:
        raise RuntimeError(f"claude salió con {res.returncode}: {res.stderr.strip()[:2000]}")
    data = json.loads(res.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude devolvió error: {data.get('result')}")
    out = data.get("structured_output")
    if out is None:
        out = json.loads(data.get("result") or "{}")
    meta = {
        "turns": data.get("num_turns"),
        "cost_usd": round(data.get("total_cost_usd", 0) or 0, 2),
        "raw_findings": len(out.get("findings", [])),
        "model": ccfg.get("model"),
    }
    log(f"  claude terminó: {meta['raw_findings']} hallazgos brutos, "
        f"{meta['turns']} turnos, {meta['cost_usd']:.2f} USD")
    return out, meta


# ----------------------------------------------------------------- hallazgos

def validate_findings(findings: list, commentable: dict) -> list:
    valid = []
    for f in findings:
        path = f["path"]
        lines = commentable.get(path)
        if not lines:
            log(f"  descartado: {path} no está en el diff a comentar")
            continue
        line, start = int(f["line"]), int(f["start_line"])
        if line not in lines:
            near = [l for l in lines if abs(l - line) <= 3]
            if not near:
                log(f"  descartado: {path}:{line} fuera del diff")
                continue
            line = min(near, key=lambda l: abs(l - line))
        if start >= line or any(l not in lines for l in range(start, line)):
            start = line
        valid.append({**f, "line": line, "start_line": start})
    valid.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["path"], f["line"]))
    return valid[: CONFIG["max_findings"]]


def build_payload(draft: dict) -> dict:
    """Lo que se manda a GitHub, a partir del borrador (ya editado o no)."""
    comments = []
    for c in enabled_comments(draft):
        item = {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"].strip()}
        if c["start_line"] < c["line"]:
            item["start_line"] = c["start_line"]
            item["start_side"] = "RIGHT"
        comments.append(item)
    body = "\n\n".join(t for t in [praise_text(draft), (draft.get("summary") or "").strip()] if t)
    if CONFIG.get("visible_signature"):
        scope = ("PR completa" if draft["mode"] == "full"
                 else f"commits `{draft['last_sha'][:10]}..{draft['head'][:10]}`")
        body += f"\n\n_{CONFIG['visible_signature']} · {scope}_"
    # El marcador es invisible en GitHub y es lo que permite saber hasta qué commit se revisó.
    body = (body + "\n" if body else "") + f"<!-- uatu sha={draft['head']} -->"
    return {"commit_id": draft["head"], "event": "COMMENT", "body": body, "comments": comments}


def print_draft(draft: dict) -> None:
    """Enseña un borrador por consola, con el código señalado."""
    sep = "=" * 72
    print(f"\n{sep}\n{draft['repo']}#{draft['number']} · {draft['title']}\n{draft['url']}\n{sep}")
    if praise_text(draft):
        print(f"\nELOGIO\n{praise_text(draft)}")
    elif (draft.get("praise") or {}).get("body"):
        print(f"\n(elogio propuesto, no incluido por {draft['praise']['gate']}: {draft['praise']['body']})")
    if draft.get("summary"):
        print(f"\nRESUMEN\n{draft['summary']}")
    for c in enabled_comments(draft):
        start, end = c["start_line"], c["line"]
        rng = f"{start}-{end}" if start != end else str(end)
        print(f"\n{c['path']}:{rng}")
        print("```")
        for row in c.get("context") or [{"n": 0, "text": "(sin contexto)", "hit": False}]:
            mark = ">" if row["hit"] else " "
            print(f"{mark}{row['n']:>5}  {row['text']}")
        print("```")
        print(f"====\n{c['body'].strip()}\n====")
    print(f"\n{len(enabled_comments(draft))} comentarios · borrador {draft['status']}\n")


def post_review(repo: str, number: int, payload: dict) -> None:
    sh(["gh", "api", "-X", "POST", f"repos/{ORG}/{repo}/pulls/{number}/reviews",
        "--input", "-"], input_text=json.dumps(payload))


# ----------------------------------------------------------------- flujo por PR

def make_draft(pr: dict, force_full: bool = False) -> dict:
    """Revisa la PR con claude y guarda el borrador. No publica ni toca el estado."""
    repo, number, head = pr["repo"], pr["number"], pr["headRefOid"]
    key = state_key(repo, number)
    last_sha = None if force_full else last_reviewed_sha(repo, number, load_state())
    if last_sha == head:
        raise Skip(f"sin cambios desde la última revisión ({head[:10]})")

    STATE_DIR.mkdir(exist_ok=True)
    # Un clon por repo: dos revisiones del mismo repo no pueden solaparse.
    with open(STATE_DIR / f"{repo}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        repo_path = ensure_clone(repo)
        merge_base = prepare_checkout(repo_path, pr)
        full_diff = git_diff(repo_path, merge_base, head)

        mode = "full"
        if last_sha and is_ancestor(repo_path, last_sha, head):
            mode, delta_diff = "delta", git_diff(repo_path, last_sha, head)
        else:
            if last_sha:
                log(f"{key}: la rama se reescribió (force-push), revisión completa")
            delta_diff = full_diff

        changed = count_changed_lines(delta_diff)
        if changed > CONFIG["max_changed_lines"]:
            raise Skip(f"{changed} líneas cambiadas > tope {CONFIG['max_changed_lines']}")

        WORK_DIR.mkdir(parents=True, exist_ok=True)
        diff_file = WORK_DIR / f"{repo}-{number}-full.diff"
        delta_file = WORK_DIR / f"{repo}-{number}-delta.diff"
        diff_file.write_text(full_diff)
        delta_file.write_text(delta_diff)

        log(f"{key}: revisión {mode}, {changed} líneas, '{pr['title']}' por {pr['author']['login']}")
        prompt = render_prompt(pr, head, diff_file, delta_file, mode, last_sha)
        result, meta = run_claude(repo_path, prompt, pr)

        # Los comentarios sólo pueden caer en líneas del diff de la PR; en delta,
        # además, sólo en las del delta.
        findings = validate_findings(result.get("findings", []), commentable_lines(delta_diff))
        findings = [attach_agreement(f) for f in findings]
        comments = [{
            "path": f["path"], "start_line": f["start_line"], "line": f["line"],
            "severity": f["severity"], "rule": f["rule"], "body": f["body"].strip(),
            "agreement": (f.get("agreement") or "") or None,
            "enabled": True,
            "context": code_context(repo_path, f["path"], f["start_line"], f["line"]),
        } for f in findings]

    draft = {
        "repo": repo, "number": number, "title": pr["title"], "author": pr["author"]["login"],
        "url": pr["url"], "head": head, "head_ref": pr["headRefName"], "base_ref": pr["baseRefName"],
        "mode": mode, "last_sha": last_sha, "lines_changed": changed,
        "created_at": now(), "status": "pending",
        "summary": (result.get("summary") or "").strip(),
        "praise": None,
        "comments": comments, "claude": meta,
    }
    praise = (result.get("praise") or "").strip()
    if praise:
        gate = praise_gate(pr["author"]["login"])
        draft["praise"] = {"body": praise, "enabled": gate == "ok", "gate": gate}
        log(f"{key}: elogio propuesto ({'incluido' if gate == 'ok' else 'apartado por ' + gate})")
    save_draft(draft)
    log(f"{key}: borrador listo con {len(comments)} comentarios (sin publicar)")
    return draft


def publish_draft(draft: dict) -> dict:
    """Publica el borrador tal como está (con las ediciones) y marca la PR como revisada."""
    if draft["status"] != "pending":
        raise ValueError(f"el borrador ya está {draft['status']}")
    payload = build_payload(draft)
    if not payload["comments"] and not praise_text(draft) and not (draft.get("summary") or "").strip():
        raise ValueError("no hay nada que publicar: descarta el borrador o deja algún comentario")
    key = state_key(draft["repo"], draft["number"])
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / f"{draft['repo']}-{draft['number']}-payload.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False))
    post_review(draft["repo"], draft["number"], payload)
    n = len(payload["comments"])
    update_state(key, {"sha": draft["head"], "at": now(), "status": "reviewed",
                       "findings": n, "mode": draft["mode"]})
    if praise_text(draft):
        record_praise(draft["author"])
    draft.update({"status": "published", "published_at": now(), "published_comments": n})
    save_draft(draft)
    log(f"{key}: publicados {n} comentarios{' + elogio' if praise_text(draft) else ''} en {draft['url']}")
    return draft


def discard_draft(draft: dict) -> dict:
    """No publica nada y da la PR por revisada hasta el commit del borrador."""
    if draft["status"] != "pending":
        raise ValueError(f"el borrador ya está {draft['status']}")
    key = state_key(draft["repo"], draft["number"])
    update_state(key, {"sha": draft["head"], "at": now(), "status": "discarded",
                       "findings": 0, "mode": draft["mode"]})
    draft.update({"status": "discarded", "discarded_at": now()})
    save_draft(draft)
    log(f"{key}: borrador descartado, nada publicado")
    return draft


def pending_rows(rows: list) -> list:
    """PRs que necesitan revisión y no tienen ya un borrador pendiente para su head."""
    out = []
    for r in rows:
        if r["status"] == "al-dia":
            continue
        d = r["draft"]
        if d and d["status"] == "pending" and not d["stale"]:
            continue
        out.append(r)
    return out


# ----------------------------------------------------------------- comandos

def cmd_run(args) -> None:
    rows = overview()
    if args.repo:
        rows = [r for r in rows if r["repo"] == args.repo]
    todo = pending_rows(rows)
    budget = args.max or CONFIG["max_prs_per_run"]
    log(f"run: {len(rows)} candidatas, {len(todo)} por revisar; tope {budget}")
    done = 0
    for r in todo[:budget]:
        try:
            make_draft(get_pr(r["repo"], r["number"]))
            done += 1
        except Skip as skip:
            log(f"{r['key']}: se salta, {skip}")
        except Exception as exc:  # una PR rota no para el resto
            log(f"{r['key']}: ERROR {exc}")
    log(f"run terminado: {done} borradores nuevos. Revísalos en la web o con `publish`")


def cmd_review(args) -> None:
    try:
        draft = make_draft(get_pr(args.repo, args.pr), force_full=args.full)
    except Skip as skip:
        log(f"{args.repo}#{args.pr}: se salta, {skip}")
        return
    print_draft(draft)
    if args.publish:
        publish_draft(draft)


def cmd_publish(args) -> None:
    draft = load_draft(args.repo, args.pr)
    if not draft:
        sys.exit(f"no hay borrador para {args.repo}#{args.pr}")
    print_draft(draft)
    publish_draft(draft)


def cmd_approve(args) -> None:
    try:
        res = approve_pr(args.repo, args.pr, args.message or "")
    except ValueError as err:
        sys.exit(f"{args.repo}#{args.pr}: no se aprueba, {err}")
    print(f"{res['key']} aprobada en {res['sha'][:10]}")


def cmd_replies(args) -> None:
    """Enseña por consola los hilos de mis comentarios con sus respuestas."""
    conv = get_conversation(args.repo, args.pr)
    print(f"{conv['key']} · {conv['title']}\n{conv['url']}")
    if conv["approval"]:
        print(f"aprobada por {ME} el {conv['approval']['at']} en {(conv['approval']['sha'] or '')[:10]}")
    for t in conv["threads"]:
        flags = " ".join(f for f, on in (("resuelto", t["resolved"]), ("obsoleto", t["outdated"]),
                                         ("por contestar", t["awaiting_me"])) if on)
        print(f"\n{t['path']}:{t['line']} {flags}")
        print(f"  {ME}: {t['comment']['body'].strip()}")
        for r in t["replies"]:
            print(f"  ↳ {r['author']} ({r['at'][:16]}): {r['body'].strip()}")
    s = conv["summary"]
    print(f"\n{s['threads']} hilos · {s['replies']} respuestas · {s['awaiting_me']} por contestar"
          + (f" · {'se puede aprobar' if conv['can_approve'] else conv['approve_blocked']}"))


def cmd_status(args) -> None:
    labels = {"al-dia": "al día", "nuevos-commits": "nuevos commits", "pendiente": "pendiente"}
    for r in overview():
        st = labels[r["status"]]
        if r["last"] and r["status"] == "al-dia":
            st += f" ({r['last']['status']})"
        d = r["draft"]
        if d and d["status"] == "pending":
            st += f" · borrador {d['comments']} com." + (" OBSOLETO" if d["stale"] else "")
        extra = []
        if r["approval"]:
            extra.append("APROBADA" + ("*" if r["approval"]["sha"] != r["head"] else ""))
        c = r["conversation"]
        if c and c["replies"]:
            extra.append(f"{c['replies']} resp." + (f" ({c['new']} nuevas)" if c["new"] else ""))
        print(f"{r['key']:<16} {st:<36} {' '.join(extra):<24} {r['author']:<22} {r['title'][:50]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="genera borradores para las PRs pendientes")
    p.add_argument("--repo", help="limita a un repo")
    p.add_argument("--max", type=int, help="tope de PRs en esta ejecución")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("review", help="revisa una PR concreta y deja el borrador")
    p.add_argument("repo")
    p.add_argument("pr", type=int)
    p.add_argument("--full", action="store_true", help="ignora la revisión anterior y revisa toda la PR")
    p.add_argument("--publish", action="store_true", help="publica el borrador sin pasar por la web")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("publish", help="publica un borrador existente")
    p.add_argument("repo")
    p.add_argument("pr", type=int)
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("approve", help="aprueba una PR ya revisada hasta su commit actual")
    p.add_argument("repo")
    p.add_argument("pr", type=int)
    p.add_argument("-m", "--message", help="cuerpo opcional de la review de aprobación")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("replies", help="enseña las respuestas a mis comentarios en una PR")
    p.add_argument("repo")
    p.add_argument("pr", type=int)
    p.set_defaults(fn=cmd_replies)
    p = sub.add_parser("status", help="muestra el estado de cada PR candidata (APROBADA* = commits después del approve)")
    p.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
