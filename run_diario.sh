#!/bin/bash
# Wrapper manual del pronóstico diario WRF (v1). 
#
# Carga el entorno del proyecto y ejecuta scripts/pipeline_diario.py registrando
# el log del día. Pasos:
#   1) Observaciones EcoWitt del día  -> OBS_DOMAIN101
#   2) GFS <ciclo>Z (0..36h) -> WPS -> real.exe -> wrf.exe nudged
#   3) Producto de pronóstico (T2/RH/PSFC/viento) para mañana + informe
#
# Uso:
#   ./run_diario.sh [--fecha YYYY-MM-DD] [--ciclo HH] [--run-hours N] [--dry-run] [opciones de pipeline_diario]
#
# Ejemplos:
#   ./run_diario.sh                           # hoy 12Z, 36h, con asimilación
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

FECHA=""
while [ $# -gt 0 ]; do
    case "$1" in
        --fecha) FECHA="$2"; shift 2 ;;
        *) break ;;
    esac
done

# Recolectar todos los argumentos para pasarlos a pipeline_diario.py
ARGS=("$@")
if [ -z "$FECHA" ]; then
    FECHA="$(date -u +%Y-%m-%d)"
    ARGS+=("--fecha" "$FECHA")
else
    ARGS+=("--fecha" "$FECHA")
fi

LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG="$(date -u +$LOG_DIR/run_${FECHA}_%Y%m%d_%H%M%S.log)"

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