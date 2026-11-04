#!/bin/zsh
# Arranca/para la web de UATU en segundo plano.
#
#   ./web.sh start [--local] [--port 8787]   # en la red local; --local sólo este Mac
#   ./web.sh stop
#   ./web.sh restart [--local] [--port N]
#   ./web.sh status
#   ./web.sh log                              # sigue el log del servidor
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE=state/server.pid
OUT=logs/server.out.log
PORT=8787
HOST=0.0.0.0
CMD=${1:-status}; shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) HOST=127.0.0.1 ;;
    --port) PORT=$2; shift ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
  shift
done

running_pid() {
  # pid del servidor si sigue vivo; vacío si no
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then echo "$pid"; return; fi
  pgrep -f 'python3 server.py' | head -1 || true
}

urls() {
  local port=$1 host=$2 ip
  echo "  local:  http://127.0.0.1:$port/"
  if [[ "$host" != "127.0.0.1" ]]; then
    for i in en0 en1 en5 en6; do
      ip=$(ipconfig getifaddr $i 2>/dev/null || true)
      [[ -n "$ip" ]] && echo "  red:    http://$ip:$port/   ($i)"
    done
  fi
}

start() {
  local pid; pid=$(running_pid)
  if [[ -n "$pid" ]]; then echo "ya está arrancado (pid $pid)"; status; return; fi
  mkdir -p state logs
  # Sesión propia (setsid): sobrevive al cierre del terminal y a que maten el grupo de procesos.
  python3 - "$HOST" "$PORT" "$OUT" "$PIDFILE" <<'PYL'
import subprocess, sys
host, port, out, pidfile = sys.argv[1:]
p = subprocess.Popen(["python3", "server.py", "--host", host, "--port", port, "--no-open"],
                     stdin=subprocess.DEVNULL, stdout=open(out, "ab"), stderr=subprocess.STDOUT,
                     start_new_session=True)
open(pidfile, "w").write(str(p.pid))
PYL
  sleep 1.5
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "no arrancó; últimas líneas de $OUT:" >&2; tail -5 "$OUT" >&2; exit 1
  fi
  echo "web UATU arrancada (pid $(cat "$PIDFILE"), escuchando en $HOST:$PORT)"
  urls "$PORT" "$HOST"
  [[ "$HOST" != "127.0.0.1" ]] && echo "  aviso: sin autenticación; sólo en redes de confianza"
  return 0
}

stop() {
  local pid; pid=$(running_pid)
  if [[ -z "$pid" ]]; then echo "no está arrancado"; rm -f "$PIDFILE"; return; fi
  # Si hay una revisión en curso se pierde la cola (los borradores ya hechos quedan en disco).
  kill "$pid" && sleep 1
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
  rm -f "$PIDFILE"
  echo "web UATU parada (pid $pid)"
}

status() {
  local pid; pid=$(running_pid)
  if [[ -z "$pid" ]]; then echo "web UATU: parada"; return 1; fi
  local listen; listen=$(lsof -nP -a -p "$pid" -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | head -1)
  echo "web UATU: en marcha (pid $pid, $listen)"
  local port host
  port=${listen##*:}; host=${listen%%:*}
  urls "$port" "$host"
  local jobs
  jobs=$(curl -s "http://127.0.0.1:$port/api/jobs" | python3 -c '
import json, sys
busy = [j for j in json.load(sys.stdin)["jobs"].values() if j["status"] in ("queued", "running")]
print("  cola: " + (", ".join("%s (%s)" % (j["key"], j["status"]) for j in busy) if busy else "vacía"))' 2>&1 || true)
  [[ -n "$jobs" ]] && echo "$jobs"
  return 0
}

case "$CMD" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  log) tail -n 30 -f "$OUT" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
