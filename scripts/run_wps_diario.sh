#!/bin/bash
# WPS diario para el dominio San Juan: ungrib + metgrid (geogrid se reutiliza).
#
# Uso:
#   ./scripts/run_wps_diario.sh YYYY-MM-DD HH [HORAS]   (ej: ./scripts/run_wps_diario.sh 2026-10-13 12 36)
#
# Requisitos:
#   - GFS descargado en wps_sanjuan/gfs_data/<FECHA>_<HH>/ (gfs.t<HH>z.pgrb2.0p25.f000..f<HORAS>)
#   - geogrid no se re-ejecuta si existe geo_em.d01.nc (estático del dominio)
#
# Salida:
#   wps_sanjuan/met_em.d01.*.nc  (forzante para real.exe)

set -e

FECHA=$1
HORA=$2
HORAS=${3:-36}

if [ -z "$FECHA" ] || [ -z "$HORA" ]; then
    echo "Uso: $0 YYYY-MM-DD HH [HORAS]"
    exit 1
fi

START_DATE="${FECHA}_${HORA}:00:00"
END_DATE=$(date -d "${FECHA} ${HORA}:00:00 ${HORAS} hours" +%Y-%m-%d_%H:00:00)

# Rutas WRF/WPS (PC pgich); override con env vars (ver .env)
export WRF_BASE=${WRF_BASE:-/home/pgich/Build_WRF}
export WPS_DIR=${WPS_DIR:-/home/pgich/Build_WRF/WPS}
export WRF_EJECUTABLES=${WRF_EJECUTABLES:-/home/pgich/wrf-operativo/ejecutables}
export GEOG_DATA_PATH=${GEOG_DATA_PATH:-/home/pgich/wrf-operativo/datos/WPS_GEOG}
source ${WRF_EJECUTABLES}/env.bash 2>/dev/null || true

cd "$(dirname "$0")/../wps_sanjuan"

# Vincular ejecutables y tablas de la instalación WPS si no están presentes
for bin in geogrid.exe metgrid.exe ungrib.exe link_grib.csh; do
    [ -e "$bin" ] || ln -sf "$WPS_DIR/$bin" "$bin"
done
[ -e GEOGRID.TBL ] || ln -sf "$WPS_DIR/geogrid/GEOGRID.TBL" GEOGRID.TBL
[ -e METGRID.TBL ] || ln -sf "$WPS_DIR/metgrid/METGRID.TBL" METGRID.TBL
[ -e Vtable ] || ln -sf "$WPS_DIR/ungrib/Variable_Tables/Vtable.GFS" Vtable

# Namelist.wps con las fechas del ciclo
sed -e "s/START_DATE/${START_DATE}/g" \
    -e "s/END_DATE/${END_DATE}/g" \
    -e "s|GEOG_DATA_PATH|${GEOG_DATA_PATH}|g" \
    namelist.wps.template > namelist.wps
echo "start_date = ${START_DATE} / end_date = ${END_DATE}"
echo "geog_data_path = ${GEOG_DATA_PATH}"

if [ ! -f geo_em.d01.nc ]; then
    echo "geogrid.exe (generando geo_em.d01.nc estático)..."
    ./geogrid.exe
else
    echo "geogrid omitido: geo_em.d01.nc ya existe"
fi

echo "ungrib.exe..."
ls gfs_data/${FECHA}_${HORA}/ > /dev/null 2>&1 || { echo "No hay GFS en gfs_data/${FECHA}_${HORA}/"; exit 1; }
rm -f FILE:* GRIBFILE.*
./link_grib.csh gfs_data/${FECHA}_${HORA}/
./ungrib.exe

echo "metgrid.exe..."
./metgrid.exe

echo "WPS DIARIO COMPLETADO (${HORAS}h)"
ls -la met_em.d01.*