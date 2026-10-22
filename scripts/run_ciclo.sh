#!/bin/bash
# Ejecuta un ciclo diario (00/06/12/18 UTC) con el protocolo de asimilación hasta t0.
# Lo usan los timers systemd; también sirve a mano: ./scripts/run_ciclo.sh 12
#
# La fecha GFS es la del ciclo. Si el ciclo todavía no ocurrió hoy (p. ej. el timer del
# 12Z dispara a la 01:30 UTC del día siguiente), se usa la fecha de ayer.
# El día objetivo es siempre el día local siguiente a la fecha del ciclo.

set -euo pipefail

CICLO="${1:?uso: $0 HH  (00|06|12|18)}"
case "$CICLO" in
    00|06|12|18) ;;
    *) echo "Ciclo inválido: $CICLO (usar 00, 06, 12 o 18)" >&2; exit 2 ;;
esac
shift

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export TZ=UTC

HORA_UTC="$(date -u +%H)"
FECHA="$(date -u +%Y-%m-%d)"
if [ $((10#$HORA_UTC)) -lt $((10#$CICLO)) ]; then
    FECHA="$(date -u -d yesterday +%Y-%m-%d)"
fi
OBJETIVO="$(date -u -d "${FECHA} +1 day" +%Y-%m-%d)"

echo "Ciclo ${CICLO}Z | fecha GFS ${FECHA} | día objetivo local ${OBJETIVO} | ahora ${HORA_UTC}Z"
exec ./run_diario.sh --fecha "$FECHA" --ciclo "$CICLO" --asim-hours 12 \
    --dia-objetivo "$OBJETIVO" --holdout "$@"
