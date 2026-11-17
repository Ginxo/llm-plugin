#!/usr/bin/env bash
# Spike S13 (parte de Codex CLI): ¿puede Codex CLI identificar al usuario ante el proxy de BaaS-AI-IP
# con la cabecera X-Baas-Api-Key? Ver docs/spike-s13.md.
#
#   PROXY_URL=http://proxy.localhost:8080 APP_URL=http://app.localhost:8080 dev/spike_s13_codex.sh
#
# - Instala Codex CLI con npm en un directorio temporal (o usa CODEX_BIN) y usa CODEX_HOME y HOME temporales.
# - Codex se ejecuta en espacios de nombres de red y de montaje propios (unshare -rnm): solo llega al puerto del proxy
#   a través de dev/netns_bridge.py. Nada sale a Internet ni se usa ninguna cuenta de OpenAI; la credencial
#   del proveedor es ficticia.
# - El proxy debe apuntar a un OpenAI falso con la API Responses (dev/fake_openai.py, vía
#   BAAS_DEV_OPENAI_UPSTREAM). Con FAKE_LOG_CMD (p. ej. «podman logs h2-plugin-fake-openai») muestra
#   lo que recibió el proveedor falso en cada caso.
# - Con APP_URL, lista al final las conversaciones de OpenAI visibles para el usuario (traza).
#
# Variables: PROXY_URL, APP_URL, ORG (demo), EMAIL (analista@demo.test), CODEX_BIN, CODEX_VERSION (latest),
# FAKE_LOG_CMD, KEEP=1 (conserva el directorio de trabajo).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROXY_URL="${PROXY_URL:-http://proxy.localhost:8080}"
PROXY_URL="${PROXY_URL%/}"
ORG="${ORG:-demo}"
EMAIL="${EMAIL:-analista@demo.test}"
BASE_URL="$PROXY_URL/org/$ORG/openai/v1"
PORT="$(python3 -c 'import sys,urllib.parse as u;p=u.urlsplit(sys.argv[1]);print(p.port or 80)' "$PROXY_URL")"
PROXY_HOST="$(python3 -c 'import sys,urllib.parse as u;print(u.urlsplit(sys.argv[1]).hostname)' "$PROXY_URL")"
WORK="$(mktemp -d -t baas-s13-XXXXXX)"
PY="$(command -v python3)"
BRIDGE_PID=
CASE_PGID= # grupo de procesos del caso en curso (unshare, sh, puente interno, timeout y Codex)

kill_case() {
  [[ -n "$CASE_PGID" ]] || return 0
  kill -TERM -- "-$CASE_PGID" 2>/dev/null || true
  local _
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 -- "-$CASE_PGID" 2>/dev/null || break
    sleep 0.2
  done
  kill -KILL -- "-$CASE_PGID" 2>/dev/null || true
  CASE_PGID=
}

cleanup() {
  kill_case
  [[ -n "$BRIDGE_PID" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
  if [[ "${KEEP:-0}" == 1 ]]; then echo "Directorio de trabajo: $WORK"; else rm -rf "$WORK"; fi
}
# Ctrl-C o SIGTERM: se sale por la trampa de EXIT, que mata el caso en curso antes de borrar $WORK.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

KEY="$("$PY" -c 'import base64,hashlib,sys;print("baas_"+base64.urlsafe_b64encode(hashlib.sha256(("baas-dev-key:"+sys.argv[1]).encode()).digest()).rstrip(b"=").decode())' "$EMAIL")"
FAKE_PROVIDER_KEY="sk-fake-s13-provider-key"

if [[ -z "${CODEX_BIN:-}" ]]; then
  echo "== Instalando @openai/codex@${CODEX_VERSION:-latest} en $WORK/npm"
  npm i --prefix "$WORK/npm" --no-fund --no-audit --loglevel=error "@openai/codex@${CODEX_VERSION:-latest}" >/dev/null
  CODEX_BIN="$WORK/npm/node_modules/.bin/codex"
fi
echo "== $("$CODEX_BIN" --version 2>/dev/null) · proxy $BASE_URL · usuario $EMAIL"

"$PY" "$REPO/dev/netns_bridge.py" unix-to-tcp "$WORK/bridge.sock" "127.0.0.1:$PORT" 2>/dev/null &
BRIDGE_PID=$!
sleep 0.5

provider_toml() { # $1: línea extra del proveedor (cabeceras), $2: wire_api
  cat <<EOF
[model_providers.baas]
name = "BaaS-AI-IP (proxy)"
base_url = "$BASE_URL"
env_key = "BAAS_FAKE_PROVIDER_KEY"
wire_api = "${2:-responses}"
$1
EOF
}
TOP='model = "gpt-fake"
model_provider = "baas"
check_for_update_on_startup = false'
ENV_HEADERS='env_http_headers = { "X-Baas-Api-Key" = "BAAS_API_KEY" }'
LITERAL_HEADERS="http_headers = { \"X-Baas-Api-Key\" = \"$KEY\" }"

read -r -d '' FAKE_LOG_FMT <<'EOF' || true
import json, sys
for line in sys.stdin:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if "method" in r:
        print(f"   proveedor: {r['method']} {r['path']} · X-Baas-* recibidas: {r['x_baas_headers']}"
              f" · Authorization: {r['authorization']} · originator: {r['headers'].get('originator')}"
              f" · user-agent: {str(r['headers'].get('user-agent'))[:60]} · stream: {r['stream']}")
EOF

fake_lines() { if [[ -n "${FAKE_LOG_CMD:-}" ]]; then $FAKE_LOG_CMD 2>/dev/null | wc -l; else echo 0; fi; }

# run_case <nombre> <directorio del proyecto> [argumentos de codex exec...]
run_case() {
  local name="$1" proj="$2"
  shift 2
  local out="$WORK/out-$name" before after
  mkdir -p "$out"
  before="$(fake_lines)"
  set +e
  # setsid: el caso corre en su propio grupo de procesos (su PID es el PGID), que se mata entero al
  # terminar o al interrumpir el script. timeout --foreground deja a Codex en ese mismo grupo.
  setsid env -i HOME="$WORK/home" CODEX_HOME="$WORK/codex-$name" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
    BAAS_API_KEY="$KEY" BAAS_FAKE_PROVIDER_KEY="$FAKE_PROVIDER_KEY" \
    unshare -rnm sh -c '
      mount --bind "$4" /etc/hosts
      ip link set lo up
      "$0" "$1" tcp-to-unix "127.0.0.1:$2,[::1]:$2" "$3" 2>/dev/null &
      bridge=$!
      trap "kill \"\$bridge\" 2>/dev/null" EXIT
      sleep 0.4
      shift 4
      timeout --foreground --kill-after=5 120 "$@"' "$PY" "$REPO/dev/netns_bridge.py" "$PORT" "$WORK/bridge.sock" \
    "$WORK/hosts" "$CODEX_BIN" exec --skip-git-repo-check --ephemeral --color never -s read-only -C "$proj" \
    -o "$out/last.txt" "$@" "Responde solo: hola" >"$out/stdout" 2>"$out/stderr" </dev/null &
  CASE_PGID=$!
  wait "$CASE_PGID"
  local rc=$?
  kill_case # restos del grupo (p. ej. el binario nativo si timeout solo mató al lanzador de node)
  set -e
  after="$(fake_lines)"
  echo
  echo "## $name · exit=$rc · peticiones al proveedor falso: $((after - before))"
  echo "   última respuesta: $(cat "$out/last.txt" 2>/dev/null | tr '\n' ' ' | cut -c1-160)"
  grep -v '^\s*$' "$out/stderr" | grep -iE "error|401|403|warn|unexpected|stream" | tail -n 4 | sed 's/^/   stderr: /' | cut -c1-260 || true
  if [[ -n "${FAKE_LOG_CMD:-}" && "$after" -gt "$before" ]]; then
    $FAKE_LOG_CMD 2>/dev/null | tail -n "$((after - before))" | "$PY" -c "$FAKE_LOG_FMT"
  fi
  if grep -rqs -- "$KEY" "$WORK/codex-$name" "$WORK/home"; then
    echo "   la API key aparece en: $(grep -rls -- "$KEY" "$WORK/codex-$name" "$WORK/home" | sed "s#$WORK/##" | tr '\n' ' ')"
  else
    echo "   la API key no aparece en CODEX_HOME ni en HOME"
  fi
}

new_home() { # $1: nombre del caso, $2: contenido de config.toml del usuario
  mkdir -p "$WORK/codex-$1"
  printf '%s\n' "$2" >"$WORK/codex-$1/config.toml"
}

mkdir -p "$WORK/home" "$WORK/proyecto"
# Codex es un binario estático (musl): no usa NSS y resuelve *.localhost por DNS, que no existe dentro del
# espacio de nombres. Se monta un /etc/hosts propio (solo en el espacio de nombres de montaje de Codex).
printf '127.0.0.1 localhost %s\n::1 localhost %s\n' "$PROXY_HOST" "$PROXY_HOST" >"$WORK/hosts"
git -C "$WORK/proyecto" init -q

# 1) Configuración de usuario + env_http_headers (la clave sale de una variable de entorno)
new_home 1-usuario-env-http-headers "$TOP
$(provider_toml "$ENV_HEADERS")"
run_case 1-usuario-env-http-headers "$WORK/proyecto"

# 2) Configuración de usuario + http_headers (clave literal en config.toml)
new_home 2-usuario-http-headers "$TOP
$(provider_toml "$LITERAL_HEADERS")"
run_case 2-usuario-http-headers "$WORK/proyecto"

# 3) Chat Completions
new_home 3-wire-api-chat "$TOP
$(provider_toml "$ENV_HEADERS" chat)"
run_case 3-wire-api-chat "$WORK/proyecto"

# 4) Sin cabecera: el proxy debe rechazarla (401 baas_invalid_api_key)
new_home 4-sin-cabecera "$TOP
$(provider_toml "")"
run_case 4-sin-cabecera "$WORK/proyecto"

# 5-6) Solo configuración de proyecto (.codex/config.toml), sin y con confianza (Codex 0.169 no admite
#      model_provider ni model_providers en la configuración de proyecto: lo avisa y usa OpenAI)
for p in 5-proyecto-sin-confianza 6-proyecto-con-confianza; do
  mkdir -p "$WORK/$p/.codex" "$WORK/codex-$p"
  git -C "$WORK/$p" init -q
  printf '%s\n%s\n' "$TOP" "$(provider_toml "$ENV_HEADERS")" >"$WORK/$p/.codex/config.toml"
done
run_case 5-proyecto-sin-confianza "$WORK/5-proyecto-sin-confianza"

# 6) La misma, con el proyecto marcado como de confianza en la configuración de usuario
printf '[projects."%s"]\ntrust_level = "trusted"\n' "$WORK/6-proyecto-con-confianza" \
  >"$WORK/codex-6-proyecto-con-confianza/config.toml"
run_case 6-proyecto-con-confianza "$WORK/6-proyecto-con-confianza"

# 7) Sin ficheros: todo con -c en la línea de órdenes
mkdir -p "$WORK/codex-7-linea-de-ordenes"
run_case 7-linea-de-ordenes "$WORK/proyecto" \
  -c 'model="gpt-fake"' -c 'model_provider="baas"' \
  -c "model_providers.baas={name=\"BaaS\",base_url=\"$BASE_URL\",env_key=\"BAAS_FAKE_PROVIDER_KEY\",wire_api=\"responses\",env_http_headers={\"X-Baas-Api-Key\"=\"BAAS_API_KEY\"}}"

# 8) Perfil: $CODEX_HOME/baas.config.toml sobre una configuración de usuario vacía
mkdir -p "$WORK/codex-8-perfil"
: >"$WORK/codex-8-perfil/config.toml"
printf '%s\n%s\n' "$TOP" "$(provider_toml "$ENV_HEADERS")" >"$WORK/codex-8-perfil/baas.config.toml"
run_case 8-perfil "$WORK/proyecto" -p baas

if [[ -n "${APP_URL:-}" ]]; then
  echo
  echo "## Traza: conversaciones de OpenAI de $EMAIL en $ORG"
  "$PY" - "$REPO" "$APP_URL" "$EMAIL" "$ORG" <<'EOF'
import sys, time
repo, app, email, org = sys.argv[1:5]
sys.path.insert(0, repo + "/dev")
import e2e_h2 as e
h = {"X-Requested-With": "baas-webapp"}
r = e.http("POST", app + "/api/v1/auth/login", headers=h, payload={"email": email, "password": e.DEV_PASSWORD})
c = e.cookies_from(r.headers)
s = c.get("baas_session")
for _ in range(2):
    if s:
        break
    r = e.http("POST", app + "/api/v1/auth/totp", headers={**h, "Cookie": "baas_mfa=" + c["baas_mfa"]},
               payload={"code": e.totp(e.DEV_TOTP_SECRET)})
    s = e.cookies_from(r.headers).get("baas_session")
    if not s:
        time.sleep(31 - time.time() % 30)
r = e.http("GET", f"{app}/api/v1/conversations?org={org}&limit=200", headers={"Cookie": f"baas_session={s}"})
for i in (r.json() or {}).get("items", []):
    if str(i.get("provider")).upper() == "OPENAI":
        print(f"   {i['started_at']} · {i['api_flavor']} · modelo {i['model']} · cliente {i['client_tool']}"
              f" · turnos {i['turn_count']} · tokens {i['tokens_in']}/{i['tokens_out']} · auxiliar {i['auxiliary']}")
EOF
fi
