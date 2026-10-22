#!/bin/bash
# Wrapper manual del pronóstico diario WRF (v1). 
#
# Carga el entorno del proyecto y ejecuta scripts/pipeline_diario.py registrando
# el log del día. Pasos:
#   1) Observaciones EcoWitt de la ventana de asimilación [inicio, t0] -> OBS_DOMAIN101
#   2) GFS <ciclo>Z -> WPS -> real.exe -> wrf.exe nudged (asimila hasta t0, libre después)
#   3) Producto de pronóstico (T2/RH/PSFC/viento) del día local siguiente + informe
#
# Uso:
#   ./run_diario.sh [--fecha YYYY-MM-DD] [--ciclo HH] [--asim-hours N] [--horizon-h N] [--dry-run] [opciones de pipeline_diario]
#
# Ejemplos:
#   ./run_diario.sh                           # hoy: ciclo 06Z, asimila hasta 18Z, pronostica mañana
#   ./run_diario.sh --dry-run                 # solo disponibilidad obs + GFS
#   ./run_diario.sh --continue-sin-obs        # pronóstico GFS puro si faltan obs

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# Las observaciones y las fechas del ciclo se manejan en UTC (ver pipeline_diario.py).
# Se respeta un TZ ya definido (p. ej. en .env); si no, se fija UTC.
export TZ="${TZ:-UTC}"

FECHA=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --fecha)
            if [ $# -lt 2 ]; then
                echo "Falta el valor de --fecha (YYYY-MM-DD)" >&2
                exit 2
            fi
            FECHA="$2"; shift 2 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

if [ -z "$FECHA" ]; then
    FECHA="$(date -u +%Y-%m-%d)"
fi
ARGS+=("--fecha" "$FECHA")

LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="$(date -u +"$LOG_DIR/run_${FECHA}_%Y%m%d_%H%M%S.log")"

echo "=== Pronóstico diario WRF ==="
echo "Fecha: ${FECHA} | args: ${ARGS[*]}"
echo "Log: $LOG"

PYTHON="${VALIDATION_PYTHON:-/usr/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    echo "Python no disponible: $PYTHON" >&2
    exit 1
fi

set +e
"$PYTHON" scripts/pipeline_diario.py "${ARGS[@]}" 2>&1 | tee "$LOG"
PIPELINE_RC=${PIPESTATUS[0]}
set -e

echo "=== Código de salida: $PIPELINE_RC ==="
exit "$PIPELINE_RC"