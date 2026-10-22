#!/usr/bin/env python3
"""Pronóstico diario WRF con asimilación de observaciones EcoWitt (obs nudging).

v1: ejecución manual por el operador (la automatización systemd es etapa 2).

Protocolo temporal (mismo esquema que wrf-modular-V1): asimilación hasta t0 y
pronóstico libre desde t0.

    start = <fecha> <ciclo>Z        t0 = start + asim_hours        end = start + run_hours
      |======= nudging activo ======|------- pronóstico libre -------|

Las observaciones solo existen hasta "ahora", así que el nudging solo puede corregir
el tramo [start, t0]; después el modelo corre solo. El horizonte se calcula para
cubrir el día local siguiente al de t0 (por defecto: ciclo 06Z, 12 h de asimilación
hasta 18Z y 33 h de pronóstico libre = 45 h en total).

Flujo del día:
  1. Observaciones EcoWitt de la ventana [start, t0]  -> cobertura -> CSV/JSON
  2. QC + Little_R + OBS_DOMAIN101 (ninguna obs posterior a t0)
  3. GFS <ciclo>Z (0..run_hours) -> WPS -> met_em -> run dir WRF
  4. namelist diario (start, run_hours, obs_nudge_opt=1, fdda_end = asim_hours*60)
  5. Preflight (bloqueante; incluye la separación temporal)
  6. real.exe -> wrf.exe nudged (reintentos)
  7. Valores del pronóstico (CSV) + mapas T2/RH/PSFC/viento + informe diario

Salidas: outputs/<fecha>_<ciclo>z/  {input, nudged, productos}
Logs:    logs/run_<fecha>_<ciclo>z.log

Uso:
    ./run_diario.sh                              # corrida de hoy: ciclo 06Z, 12 h de asimilación
    ./run_diario.sh --fecha 2026-10-13           # otra fecha
    ./run_diario.sh --dry-run                    # solo disponibilidad (obs + GFS)
    ./run_diario.sh --asim-hours 6 --ciclo 12    # otra ventana de asimilación / ciclo
    ./run_diario.sh --horizon-h 24               # fija el horizonte libre en vez de cubrir el día local
    ./run_diario.sh --continue-sin-obs           # pronóstico GFS puro si faltan obs
    ./run_diario.sh --skip-wrf                   # solo preparar material hasta OBS_DOMAIN101
"""

import argparse
import errno
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pipeline_diario")

# --- Rutas del proyecto ---
DATA_RAW = ROOT / "data" / "raw"
WPS_DIR = ROOT / "wps_sanjuan"
WPS_SH = ROOT / "scripts" / "run_wps_diario.sh"
ESTACIONES_JSON = ROOT / "config" / "estaciones.json"
OUT_BASE = ROOT / "outputs"
NAMLIST_BASE = ROOT / "namelist.input"

# --- Entorno WRF (override via .env) ---
RUN_DIR = Path(os.environ.get("LOCAL_WRF_DIR", "/home/pgich/wrf-modular-diario-V-/run_wrf"))
WRF_ENV_BASH = os.environ.get("WRF_ENV_BASH", "/home/pgich/wrf-operativo/ejecutables/env.bash")
MPIRUN = os.environ.get("MPIRUN", "/usr/bin/mpirun")
WRF_NP = os.environ.get("WRF_NP", "1")
VALIDATION_PYTHON = os.environ.get("VALIDATION_PYTHON", "/usr/bin/python3")

# --- Protocolo temporal: asimilación hasta t0 + pronóstico libre (override via .env) ---
# Ciclo GFS en que arranca el modelo; con 06Z y 12 h de asimilación, t0 = 18Z y la
# corrida de las 19:30 UTC ya dispone de las observaciones hasta t0.
CICLO_DEFAULT = os.environ.get("CICLO_GFS", "06")
ASIM_HOURS_DEFAULT = int(os.environ.get("ASIM_HOURS", "12"))
# Huso del "día objetivo" del pronóstico (San Juan, UTC-3, sin horario de verano).
HUSO_LOCAL_H = float(os.environ.get("HUSO_LOCAL_H", "-3"))
# Paso de las condiciones de borde (interval_seconds = 10800 en el namelist).
PASO_HORAS = 3

# --- Umbrales de cobertura observacional ---
OBS_MIN_REGISTROS = int(os.environ.get("OBS_MIN_REGISTROS", "60"))
OBS_MIN_ESTACIONES = int(os.environ.get("OBS_MIN_ESTACIONES", "3"))

GFS_URLS = {
    "aws": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{fecha}/{ciclo}/atmos/{file}",
    "nomads": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{fecha}/{ciclo}/atmos/{file}",
}

# Resolución GFS: 0p50 para los 4 ciclos diarios (0p25 queda como opción de sensibilidad).
GFS_RES = os.environ.get("GFS_RES", "0p50")
# Con 4 ciclos/día el GFS ocupa ~2-3 GB por ciclo: se borra tras generar met_em salvo KEEP_GFS=1.
KEEP_GFS = os.environ.get("KEEP_GFS", "0") == "1"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _leer_namelist_valor(namelist: Path, clave: str) -> Optional[float]:
    """Extrae el valor numérico de una clave del namelist (p. ej. max_obs, run_hours)."""
    if not namelist or not Path(namelist).exists():
        return None
    texto = Path(namelist).read_text(encoding="utf-8")
    m = re.search(rf"^\s*{clave}\s*=\s*([0-9.]+)\s*,?\s*$", texto, re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Ventana temporal (asimilación hasta t0 + pronóstico libre)
# --------------------------------------------------------------------------
def calcular_ventana(fecha: str, ciclo: str, asim_hours: int,
                     horizon_h: Optional[int] = None,
                     run_hours: Optional[int] = None,
                     dia_objetivo: Optional[str] = None) -> Dict[str, Any]:
    """Ventana temporal de la corrida. Todos los datetime son UTC naive.

        start = <fecha> <ciclo>Z       t0 = start + asim_hours       end = start + run_hours
          |====== nudging activo ======|------ pronóstico libre ------|

    - Hasta t0 se asimilan observaciones; desde t0 el modelo corre solo.
    - run_hours = asim_hours + horizonte.
    - Sin ``dia_objetivo``, el día objetivo es el día local siguiente al de t0. Con
      ``dia_objetivo`` (fecha local YYYY-MM-DD) se ancla explícitamente, lo que permite
      que los 4 ciclos pronostiquen el mismo día local.
    - Si no se fija el horizonte (--horizon-h / --run-hours), se calcula el necesario para
      llegar al fin del día objetivo, redondeado hacia arriba al paso de las condiciones
      de borde (3 h).
    """
    if asim_hours <= 0:
        raise ValueError(f"--asim-hours debe ser > 0 (recibido {asim_hours})")
    start = datetime.strptime(f"{fecha} {int(ciclo):02d}", "%Y-%m-%d %H")
    t0 = start + timedelta(hours=asim_hours)

    huso = timedelta(hours=HUSO_LOCAL_H)
    if dia_objetivo:
        dia_obj = datetime.strptime(dia_objetivo, "%Y-%m-%d").date()
    else:
        dia_obj = ((t0 + huso) + timedelta(days=1)).date()
    obj_ini = datetime.combine(dia_obj, datetime.min.time()) - huso
    obj_fin = obj_ini + timedelta(days=1)
    if t0 >= obj_fin:
        raise ValueError(f"el día objetivo {dia_obj} termina antes de t0 ({t0:%Y-%m-%d %H}Z)")

    if run_hours is not None:
        horizon = int(run_hours) - asim_hours
        if horizon_h is not None and int(horizon_h) != horizon:
            raise ValueError(f"--run-hours ({run_hours}) y --horizon-h ({horizon_h}) no son "
                             f"coherentes con --asim-hours ({asim_hours})")
    elif horizon_h is not None:
        horizon = int(horizon_h)
    else:
        horas_hasta_fin = (obj_fin - t0).total_seconds() / 3600.0
        horizon = int(math.ceil(horas_hasta_fin / PASO_HORAS) * PASO_HORAS)
    if horizon <= 0:
        raise ValueError(f"el horizonte de pronóstico libre debe ser > 0 h (resultó {horizon} h)")

    run = asim_hours + horizon
    if run % PASO_HORAS:
        raise ValueError(f"run_hours={run} (asimilación {asim_hours} h + horizonte {horizon} h) debe ser "
                         f"múltiplo de {PASO_HORAS} h: las condiciones de borde llegan cada {PASO_HORAS} h")
    end = start + timedelta(hours=run)

    # Tiempos válidos del día objetivo (cada 3 h) que caen en el pronóstico libre.
    valid_times: List[str] = []
    t = max(obj_ini, t0)
    while t <= obj_fin and t <= end:
        valid_times.append(t.strftime("%Y-%m-%d_%H:%M:%S"))
        t += timedelta(hours=PASO_HORAS)

    return {
        "start": start, "t0": t0, "end": end,
        "asim_hours": asim_hours, "horizon_h": horizon, "run_hours": run,
        "dia_objetivo": dia_obj, "objetivo_ini": obj_ini, "objetivo_fin": obj_fin,
        "cubre_dia_objetivo": end >= obj_fin,
        "valid_times": valid_times,
    }


# --------------------------------------------------------------------------
# Observaciones EcoWitt
# --------------------------------------------------------------------------
def _csv_cubre(csv_path: Path, hasta_ts: int, tolerancia_s: int = 900) -> bool:
    """True si el CSV tiene registros hasta ``hasta_ts`` (epoch UTC, con tolerancia)."""
    try:
        ts = pd.to_numeric(pd.read_csv(csv_path, usecols=["hora_unix"])["hora_unix"], errors="coerce")
        return bool(ts.max() >= hasta_ts - tolerancia_s)
    except Exception as e:
        logger.warning(f"No se pudo comprobar la cobertura de {csv_path.name}: {e}")
        return False


def descargar_obs(fecha: str, fuerza: bool = False, hasta_ts: Optional[int] = None) -> Path:
    """Descarga el histórico diario si no existe (o no llega a ``hasta_ts``) y devuelve el CSV.

    Un CSV bajado temprano (p. ej. por una corrida manual previa) queda incompleto; con
    ``hasta_ts`` se detecta y se vuelve a descargar en vez de reutilizarlo.
    """
    css = DATA_RAW / f"ecowitt_historico_{fecha.replace('-', '')}.csv"
    if not fuerza and css.exists() and css.stat().st_size > 100:
        if hasta_ts is None or _csv_cubre(css, hasta_ts):
            logger.info(f"Histórico ya descargado: {css.name}")
            return css
        logger.info(f"Histórico {css.name} no llega hasta t0: se vuelve a descargar.")
    from src.ingesta.ecowitt_client import EcowittIngestor

    logger.info(f"Descargando histórico EcoWitt de {fecha} ...")
    ing = EcowittIngestor()
    placeholder = ("TU_APPLICATION_KEY" in (ing.app_key or "")) or ("TU_API_KEY" in (ing.api_key or ""))
    if not ing.tiene_credenciales() or placeholder:
        raise RuntimeError(
            "No hay credenciales EcoWitt configuradas. Completá ECOWITT_APP_KEY/ECOWITT_API_KEY en .env"
        )
    ing.descargar_historico(fecha, output_dir=str(DATA_RAW))
    if not css.exists() or css.stat().st_size <= 100:
        raise RuntimeError(f"No se generó el CSV histórico para {fecha}. Revisar credenciales/API.")
    return css


def obtener_obs_ventana(start: datetime, t0: datetime, fuerza: bool = False) -> Path:
    """Devuelve el CSV de observaciones que cubre la ventana de asimilación [start, t0].

    Descarga los días que toca la ventana. Se incluye el día de (start - 3 h) porque el
    histórico diario de EcoWitt parece estar referido al día local de la estación (el CSV
    del 21/10 arranca a las 03:00 UTC); esto no está verificado contra la API. Si la
    ventana toca más de un día, se fusionan en ``ecowitt_historico_<fecha>_ventana.csv``.
    """
    ts_t0 = int(t0.replace(tzinfo=timezone.utc).timestamp())
    dia = (start - timedelta(hours=3)).date()
    fechas: List[str] = []
    while dia <= t0.date():
        fechas.append(dia.isoformat())
        dia += timedelta(days=1)

    csvs = [descargar_obs(f, fuerza, hasta_ts=ts_t0 if f == fechas[-1] else None) for f in fechas]
    if len(csvs) == 1:
        return csvs[0]

    df = pd.concat([pd.read_csv(c) for c in csvs], ignore_index=True)
    df = df.drop_duplicates(subset=["estacion", "hora_unix"]).sort_values(["estacion", "hora_unix"])
    destino = DATA_RAW / f"ecowitt_historico_{start.strftime('%Y%m%d')}_ventana.csv"
    df.to_csv(destino, index=False)
    logger.info(f"Ventana {fechas[0]}..{fechas[-1]} fusionada en {destino.name} ({len(df)} filas).")
    return destino


def cobertura_obs(csv_path: Path, start: datetime, t0: datetime,
                  asimilables: Optional[set] = None) -> Dict[str, Any]:
    """Mide la cobertura de observaciones válidas (temperatura) del CSV y en la ventana.

    Devuelve métricas del CSV completo, de la ventana de asimilación [start, t0] (la única
    parte que el nudging puede usar) y de esa ventana restringida a ``asimilables`` (las
    estaciones que entran a OBS_DOMAIN101 cuando hay hold-out). Sin ``asimilables`` se
    asimilan todas las estaciones con datos.
    """
    df = pd.read_csv(csv_path)
    vacio = {"dia": 0, "ventana": 0, "estaciones": 0, "por_estacion": {},
             "ventana_asim": 0, "estaciones_asim": 0, "por_estacion_asim": {}}
    if df.empty or "temp_c" not in df.columns:
        return vacio

    # La ventana de asimilación [start, t0] se interpreta en UTC (WRF trabaja en UTC).
    ts_ini = int(start.replace(tzinfo=timezone.utc).timestamp())
    ts_fin = int(t0.replace(tzinfo=timezone.utc).timestamp())
    df["ts"] = pd.to_numeric(df["hora_unix"], errors="coerce")
    df["temp_ok"] = df["temp_c"].apply(lambda v: _num(v) is not None)

    en_ventana = df[df["temp_ok"] & df["ts"].between(ts_ini, ts_fin)]
    dia = int(df["temp_ok"].sum())
    ventana = int(len(en_ventana))
    por_estacion = en_ventana.groupby("estacion")["temp_ok"].sum().astype(int).to_dict()

    asim = en_ventana if asimilables is None else en_ventana[en_ventana["estacion"].isin(asimilables)]
    por_estacion_asim = asim.groupby("estacion")["temp_ok"].sum().astype(int).to_dict()

    return {"dia": dia, "ventana": ventana, "estaciones": len(por_estacion),
            "por_estacion": por_estacion,
            "ventana_asim": int(len(asim)), "estaciones_asim": len(por_estacion_asim),
            "por_estacion_asim": por_estacion_asim}


def _estaciones_asimilables() -> Optional[set]:
    """Nombres con rol != evaluacion (las que entran a OBS_DOMAIN101 con --holdout)."""
    try:
        catalogo = json.loads(ESTACIONES_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"No se pudo leer {ESTACIONES_JSON.name} para el hold-out: {e}")
        return None
    return {n for n, meta in catalogo.items() if (meta or {}).get("rol") != "evaluacion"}


def generar_dataframe_validado(csv_path: Path, start: Optional[datetime] = None,
                               t0: Optional[datetime] = None) -> pd.DataFrame:
    """Aplica QC al CSV histórico y devuelve el DataFrame normalizado.

    Con ``start`` y ``t0`` se conservan solo los registros de [start, t0]: es el corazón del
    protocolo temporal (nada posterior a t0 llega a OBS_DOMAIN101, y lo anterior al inicio
    del modelo no se puede asimilar).
    """
    from src.calidad.cleaner import ObservacionesCleaner

    cleaner = ObservacionesCleaner(str(DATA_RAW))
    df = cleaner.procesar_csv_historico(str(csv_path))
    logger.info(f"QC: {len(df)} registros válidos tras la limpieza.")
    if start is not None and t0 is not None:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        dentro = (ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(t0))
        logger.info(f"Ventana de asimilación [{start:%Y-%m-%d %H:%M}, {t0:%Y-%m-%d %H:%M}] UTC: "
                    f"{int(dentro.sum())} registros dentro, {int((~dentro).sum())} descartados.")
        df = df[dentro].reset_index(drop=True)
    return df


def generar_obs_flat(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Serializa el DataFrame validado como lista plana de registros (usada por preflight)."""
    filas = []
    for _, r in df.iterrows():
        horas = str(r.get("hora", "")).split(":")
        if len(horas) < 2:
            continue
        try:
            hora = f"{int(horas[0]):02d}:{int(horas[1]):02d}:00"
        except (ValueError, TypeError):
            continue
        filas.append({
            "estacion": str(r.get("estacion")),
            "time_unix": int(pd.to_datetime(r.get("timestamp"), utc=True).timestamp()),
            "fecha": str(r.get("fecha")),
            "hora": hora,
            "temp": _num(r.get("temp")),
            "humedad": _num(r.get("humedad")),
            "viento": _num(r.get("viento")),
            "viento_rafaga": _num(r.get("viento_rafaga")),
            "direcc": _num(r.get("direcc")),
            "presion_relativa": _num(r.get("presion_relativa")),
            "presion_absoluta": _num(r.get("presion_absoluta")),
            "rain_daily": _num(r.get("rain_daily")),
            "solar": _num(r.get("solar")),
        })
    return filas


# --------------------------------------------------------------------------
# GFS
# --------------------------------------------------------------------------
def gfs_disponible(fecha: str, ciclo: str, fhour: int) -> Optional[str]:
    """Verifica disponibilidad del archivo pgrb2 en AWS/NOMADS. Devuelve fuente o None."""
    fstr = f"{fhour:03d}"
    file = f"gfs.t{ciclo}z.pgrb2.{GFS_RES}.f{fstr}"
    fecha_nodash = fecha.replace("-", "")
    for fuente, tpl in GFS_URLS.items():
        url = tpl.format(fecha=fecha_nodash, ciclo=ciclo, file=file)
        try:
            r = requests_head(url)
            if r == 200:
                return fuente
        except Exception as e:
            logger.debug(f"HEAD {fuente} f{fstr} fallo: {e}")
    return None


def requests_head(url: str, timeout: int = 30) -> int:
    import requests

    r = requests.head(url, timeout=timeout, allow_redirects=True)
    return r.status_code


def _tamano_esperado_gfs(fecha: str, ciclo: str, fhour: int, fuente: str = "aws") -> Optional[int]:
    fstr = f"{fhour:03d}"
    file = f"gfs.t{ciclo}z.pgrb2.{GFS_RES}.f{fstr}"
    url = GFS_URLS.get(fuente, GFS_URLS["aws"]).format(fecha=fecha.replace("-", ""), ciclo=ciclo, file=file)
    import requests

    try:
        r = requests.head(url, timeout=30, allow_redirects=True)
        if r.status_code == 200:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        pass
    return None


def verificar_gfs_completo(fecha: str, ciclo: str, run_hours: int) -> Dict[str, Any]:
    """Comprueba que estén disponibles todos los fhoras 0..run_hours del ciclo."""
    horas = list(range(0, run_hours + 1, 3))
    faltantes = []
    fuente = None
    for fh in horas:
        src = gfs_disponible(fecha, ciclo, fh)
        if src is None:
            faltantes.append(fh)
        elif fuente is None:
            fuente = src
    return {"completo": not faltantes, "faltantes": faltantes, "fuente": fuente, "fhoras": horas}


def descargar_gfs(fecha: str, ciclo: str, run_hours: int, fuente: str = "aws") -> int:
    """Descarga el ciclo GFS 0..run_hours hacia wps_sanjuan/gfs_data/<fecha>_<ciclo>.

    Devuelve cuántos de los ficheros esperados quedaron presentes. Se cuentan por
    nombre exacto (no con glob) para no contabilizar fhoras de corridas previas.
    """
    from src.ingesta.gfs_downloader import GFSDownloader

    out = WPS_DIR / "gfs_data" / f"{fecha}_{ciclo}"
    horas = list(range(0, run_hours + 1, 3))
    # Eliminar archivos truncados (descargas interrumpidas)
    for fh in horas:
        fname = f"gfs.t{ciclo}z.pgrb2.{GFS_RES}.f{fh:03d}"
        p = out / fname
        if not p.exists():
            continue
        esperado = _tamano_esperado_gfs(fecha, ciclo, fh, fuente)
        if esperado and abs(p.stat().st_size - esperado) > 0.02 * esperado:
            logger.warning(f"GFS corrupto ({p.stat().st_size} vs {esperado} B): re-descargando {fname}")
            p.unlink()
    GFSDownloader(str(out), res=GFS_RES).descargar_ciclo(fecha, ciclo_hora=ciclo, horas_pronostico=horas, fuente=fuente)
    return sum(1 for fh in horas if (out / f"gfs.t{ciclo}z.pgrb2.{GFS_RES}.f{fh:03d}").exists())


# --------------------------------------------------------------------------
# WPS
# --------------------------------------------------------------------------
def ejecutar_wps(fecha: str, ciclo: str, run_hours: int, timeout: int = 7200) -> Tuple[bool, str]:
    if not WPS_SH.exists():
        logger.error(f"No existe el script de WPS: {WPS_SH}")
        return False, str(WPS_SH)
    log = ROOT / "logs" / f"wps_{fecha.replace('-', '')}_{ciclo}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    try:
        with open(log, "w", encoding="utf-8") as f:
            r = subprocess.run(
                ["bash", str(WPS_SH), fecha, ciclo, str(run_hours)],
                env=env, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT, timeout=timeout,
            )
        return r.returncode == 0, str(log)
    except subprocess.TimeoutExpired:
        logger.error(f"WPS excedió el timeout ({timeout}s)")
        return False, str(log)


def propagar_metem(fecha: str, ciclo: str, run_hours: int) -> int:
    """Copia a real.exe los met_em del ciclo [start, start+run_hours]."""
    start_dt = datetime.strptime(f"{fecha} {ciclo}", "%Y-%m-%d %H")
    end_dt = start_dt + timedelta(hours=run_hours)
    n = 0
    for src in sorted(WPS_DIR.glob("met_em.d01.*.nc")):
        marca = src.name.removeprefix("met_em.d01.").removesuffix(".nc")
        try:
            ts = datetime.strptime(marca, "%Y-%m-%d_%H:%M:%S")
        except ValueError:
            continue
        if start_dt <= ts <= end_dt:
            shutil.copy2(src, RUN_DIR / src.name)
            n += 1
    return n


# --------------------------------------------------------------------------
# Namelist y OBS_DOMAIN101
# --------------------------------------------------------------------------
def preparar_namelist(namelist_src: Path, obs_nudge_opt: int, output_path: Path,
                      start_dt: datetime, run_hours: int, coefs: Dict[str, float],
                      asim_hours: Optional[int] = None) -> Path:
    from src.modelo.namelist_manager import NamelistManager

    mgr = NamelistManager(template_path=str(namelist_src))
    end_dt = start_dt + timedelta(hours=run_hours)
    mgr.actualizar_fechas(
        start_year=start_dt.year, start_month=start_dt.month, start_day=start_dt.day,
        start_hour=start_dt.hour,
        end_year=end_dt.year, end_month=end_dt.month, end_day=end_dt.day, end_hour=end_dt.hour,
        run_hours=run_hours,
    )
    # fdda_end = minutos con nudging activo. Con el protocolo temporal el nudging corre solo
    # durante la ventana de asimilación (start -> t0); después el pronóstico es libre.
    fdda_end_min = (asim_hours if asim_hours is not None else run_hours) * 60
    mgr.contenido = re.sub(r"fdda_end\s*=\s*\d+", f"fdda_end = {fdda_end_min}", mgr.contenido)
    mgr.configurar_fdda(obs_nudge_opt=obs_nudge_opt, **coefs)
    mgr.guardar(str(output_path))
    logger.info(f"Namelist diario: {output_path.name} (start {start_dt}, run_hours={run_hours}, "
                f"obs_nudge_opt={obs_nudge_opt}, fdda_end={fdda_end_min} min)")
    return output_path


def leer_coefs_base(namelist_src: Path, args) -> Dict[str, float]:
    """Coeficientes efectivos: los del namelist base salvo override por CLI."""
    coefs: Dict[str, float] = {}
    for p, arg_name in [
        ("obs_coef_wind", "obs_coef_wind"),
        ("obs_coef_temp", "obs_coef_temp"),
        ("obs_coef_mois", "obs_coef_mois"),
        ("obs_twindo", "obs_twindo"),
    ]:
        val = getattr(args, arg_name, None)
        if val is None:
            base = _leer_namelist_valor(namelist_src, p)
            if base is not None:
                coefs[p] = float(base)
        else:
            coefs[p] = float(val)
    return coefs


def _contar_obsdomain(path: Path) -> int:
    """Cuenta las observaciones de OBS_DOMAIN101 (FORMAT 105: 5 líneas por observación)."""
    if not path.exists():
        return 0
    lineas = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return len(lineas) // 5


def generar_obsdomain(df: pd.DataFrame, out_dir: Path, holdout: bool = False) -> Tuple[Path, int]:
    from src.asimilacion.littler_writer import LittleRWriter
    from src.asimilacion.obsnud_writer import ObsNudWriter

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = df["fecha"].iloc[0].replace("-", "") + "_" + str(df["hora"].iloc[0]).replace(":", "")
    littler_path, num_littler = LittleRWriter(config_estaciones_path=str(ESTACIONES_JSON)).generar_littler(
        df, output_path=str(out_dir / f"littler_{ts}.txt")
    )
    obsdomain = ObsNudWriter(config_estaciones_path=str(ESTACIONES_JSON)).generar_desde_dataframe(
        df, output_path=str(out_dir / "OBS_DOMAIN101"), usar_roles=holdout
    )
    # num_obs mide el archivo que efectivamente se asimila (OBS_DOMAIN101).
    num_obs = _contar_obsdomain(Path(obsdomain))
    logger.info(f"Little_R: {littler_path.name} ({num_littler} obs). "
                f"OBS_DOMAIN101: {obsdomain.name} ({num_obs} obs asimilables).")
    return Path(obsdomain), num_obs


# --------------------------------------------------------------------------
# Ejecución WRF
# --------------------------------------------------------------------------
def check_run_dir(run_dir: Path) -> bool:
    if not run_dir.exists():
        logger.error(f"Directorio de corrida no existe: {run_dir}")
        return False
    for exe in ("wrf.exe", "real.exe"):
        if not (run_dir / exe).exists():
            logger.error(f"No se encuentra {exe} en {run_dir}")
            return False
    # Tablas de física y datos estáticos que wrf.exe lee del CWD (LANDUSE.TBL, etc.)
    fuente = Path(os.environ.get("WRF_RUN_TPL", "/home/pgich/Build_WRF/WRF/run"))
    if fuente.is_dir():
        for item in fuente.iterdir():
            if item.is_dir() or item.name.endswith(".exe") or item.name.startswith("namelist"):
                continue
            destino = run_dir / item.name
            if not destino.exists() and not destino.is_symlink():
                destino.symlink_to(item.resolve())
    # ghg_input=1 (Registry) exige CAMtr_volume_mixing_ratio en el CWD de wrf.exe
    if not (run_dir / "CAMtr_volume_mixing_ratio").exists():
        camtr = Path(os.environ.get(
            "WRF_CAMTR_FILE",
            "/home/pgich/Build_WRF/WRF/test/em_real/CAMtr_volume_mixing_ratio",
        ))
        if camtr.exists():
            (run_dir / "CAMtr_volume_mixing_ratio").symlink_to(camtr.resolve())
            logger.info(f"CAMtr_volume_mixing_ratio enlazado en {run_dir}")
        else:
            logger.error("Falta CAMtr_volume_mixing_ratio y no se encontró la fuente "
                         f"({camtr}); ghg_input=1 abortará wrf.exe")
            return False
    return True


def ejecutar_real(run_dir: Path, timeout: int = 7200) -> bool:
    from src.modelo.wrf_runner import WRFRunner

    runner = WRFRunner(run_dir=str(run_dir), env_bash=WRF_ENV_BASH, mpirun=MPIRUN,
                       np=WRF_NP, timeout=timeout)
    return runner.ejecutar_real()


def ejecutar_wrf(run_dir: Path, timeout: int = 7200, reintentos: int = 5) -> bool:
    from src.modelo.wrf_runner import WRFRunner

    runner = WRFRunner(run_dir=str(run_dir), env_bash=WRF_ENV_BASH, mpirun=MPIRUN,
                       np=WRF_NP, timeout=timeout, reintentos=reintentos)
    return runner.ejecutar_wrf(nudged=True, run_label="nudged")


def copiar_wrfout(run_dir: Path, dest_dir: Path) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for src in sorted(run_dir.glob("wrfout_d01_*")):
        shutil.copy2(src, dest_dir / src.name)
        n += 1
    logger.info(f"Copiados {n} wrfout a {dest_dir}")
    return n


# --------------------------------------------------------------------------
# Producto e informe
# --------------------------------------------------------------------------
def generar_producto(nudged_dir: Path, productos_dir: Path, obs_json: Optional[Path],
                     valid_times: Optional[List[str]] = None) -> Tuple[int, str]:
    cmd = [sys.executable, str(ROOT / "scripts" / "generar_producto.py"),
           "--nudged-dir", str(nudged_dir),
           "--output", str(productos_dir),
           "--huso-local", str(HUSO_LOCAL_H)]
    if obs_json:
        cmd += ["--obs-json", str(obs_json), "--estaciones-json", str(ESTACIONES_JSON)]
    for t in (valid_times or []):
        cmd += ["--valid-time", t]
    logger.info(f"Producto: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        det = r.stderr.strip() or r.stdout.strip()
        logger.error(f"  Error en generación de producto: {det[-800:]}")
        return r.returncode, det[-800:]
    for line in r.stdout.splitlines()[-20:]:
        logger.info(f"  {line}")
    return 0, ""


def extraer_valores(nudged_dir: Path, caso_dir: Path, t0: datetime) -> Tuple[int, str]:
    """CSV con los valores del pronóstico, marcando qué filas son asimilación y cuáles pronóstico libre."""
    cmd = [sys.executable, str(ROOT / "scripts" / "extraer_valores.py"),
           "--nudged-dir", str(nudged_dir),
           "--estaciones-json", str(ESTACIONES_JSON),
           "--output", str(caso_dir),
           "--t0", t0.strftime("%Y-%m-%d_%H:%M:%S"),
           "--huso-local", str(HUSO_LOCAL_H)]
    logger.info(f"Valores: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        det = r.stderr.strip() or r.stdout.strip()
        logger.error(f"  Error al extraer valores: {det[-800:]}")
        return r.returncode, det[-800:]
    for line in r.stdout.splitlines()[-3:]:
        logger.info(f"  {line}")
    return 0, ""


def escribir_informe(caso_dir: Path, estado: Dict[str, Any], sufijo: str = ""):
    nombre = f"_{sufijo}" if sufijo else ""
    md = caso_dir / f"informe{nombre}.md"
    lines = [f"# Pronóstico diario WRF - {estado['fecha']} {estado['ciclo']}Z",
             "",
             f"**Generado:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)",
             f"**Estado:** {estado['estado']}",
             f"**Detalle:** {estado.get('detalle', '')}",
             "",
             "## Configuración",
             "| Parámetro | Valor |",
             "|-----------|-------|",
             f"| Inicio del modelo | {estado['fecha']} {estado['ciclo']}Z |",
             f"| Fin de asimilación (t0) | {estado.get('t0', '-')} UTC |",
             f"| Asimilación | {estado.get('asim_hours', '-')} h |",
             f"| Pronóstico libre | {estado.get('horizon_h', '-')} h |",
             f"| Duración total | {estado.get('run_hours', '-')} h |",
             f"| Día objetivo (local) | {estado.get('dia_objetivo_local', '-')} |",
             f"| Obs nudging | obs_nudge_opt={estado.get('obs_nudge_opt', 1)} |",
             f"| Hold-out | {'sí (rol=evaluacion excluido)' if estado.get('holdout') else 'no'} |",
             f"| Fuente GFS | {estado.get('fuente_gfs', '-')} |",
             f"| wrfout | {estado.get('n_wrfout', 0)} |",
             "",
             "## Observaciones",
             "",
             f"- Registros válidos del día: **{estado.get('obs_dia', 0)}**",
             f"- En la ventana [inicio, t0]: **{estado.get('obs_ventana', 0)}** registros en "
             f"**{estado.get('obs_estaciones', 0)}** estaciones",
             f"- Asimiladas en OBS_DOMAIN101: **{estado.get('obs_ventana_asim', 0)}** registros en "
             f"**{estado.get('obs_estaciones_asim', 0)}** estaciones",
             "",
             f"## Resultados",
             f"`{caso_dir}`",
             ""]
    md.write_text("\n".join(lines), encoding="utf-8")

    with open(caso_dir / f"estado{nombre}.json", "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Informe diario escrito: {md.name}")


# --------------------------------------------------------------------------
# Orquestación principal
# --------------------------------------------------------------------------
class BloqueoCorrida:
    """Bloqueo por fichero para impedir corridas simultáneas sobre el run dir WRF.

    El run dir de WRF es compartido: dos corridas concurrentes (p. ej. el timer y
    una ejecución manual) se pisarían namelist.input/OBS_DOMAIN101 y los wrfout.
    """

    def __init__(self, path: Path, stale_horas: float = 6.0):
        self.path = Path(path)
        self.stale_horas = stale_horas
        self.tomado = False

    def _es_obsoleto(self) -> bool:
        try:
            edad_h = (time.time() - self.path.stat().st_mtime) / 3600.0
        except OSError:
            return False
        return edad_h > self.stale_horas

    def _pid_vivo(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as e:
            # EPERM: el proceso existe pero es de otro usuario -> sigue vivo.
            return e.errno == errno.EPERM

    def adquirir(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n".encode())
                os.close(fd)
                self.tomado = True
                return True
            except FileExistsError:
                if self._es_obsoleto() or not self._pid_vivo():
                    logger.warning(f"Bloqueo obsoleto en {self.path}: se elimina y se reintenta.")
                    try:
                        self.path.unlink()
                    except OSError:
                        return False
                    continue
                return False
        return False

    def liberar(self) -> None:
        if self.tomado:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.tomado = False


def pipeline(args) -> int:
    """Envuelve la corrida real: bloqueo de concurrencia y registro de fallos no controlados."""
    fecha = args.fecha
    ciclo = f"{int(args.ciclo):02d}"
    caso_dir = OUT_BASE / f"{fecha}_{ciclo}z"
    lock = BloqueoCorrida(ROOT / "logs" / "corrida.lock")

    # El modo --dry-run no toca el run dir WRF: no necesita el bloqueo.
    if not args.dry_run and not lock.adquirir():
        det = (f"otra corrida parece estar en curso (bloqueo {lock.path}). "
               "Si es un bloqueo obsoleto, borrá el archivo y reintentá.")
        logger.error(det)
        escribir_informe(caso_dir, {"fecha": fecha, "ciclo": ciclo, "estado": "BLOQUEADO", "detalle": det})
        return 2

    try:
        return _pipeline(args)
    except KeyboardInterrupt:
        logger.error("Corrida interrumpida por el operador.")
        return 130
    except Exception as e:
        logger.exception("Fallo no controlado en el pipeline")
        escribir_informe(caso_dir, {
            "fecha": fecha, "ciclo": ciclo, "estado": "FALLIDO",
            "detalle": f"excepción no controlada: {type(e).__name__}: {e}",
        })
        return 1
    finally:
        lock.liberar()


def _pipeline(args) -> int:
    fecha = args.fecha
    ciclo = f"{int(args.ciclo):02d}"
    vent = calcular_ventana(fecha, ciclo, args.asim_hours, args.horizon_h, args.run_hours,
                            dia_objetivo=args.dia_objetivo)
    start_dt, t0, run_hours = vent["start"], vent["t0"], vent["run_hours"]
    logger.info(f"Protocolo temporal: start {start_dt:%Y-%m-%d %H}Z | t0 {t0:%Y-%m-%d %H}Z "
                f"(asimilación {vent['asim_hours']} h) | pronóstico libre {vent['horizon_h']} h "
                f"hasta {vent['end']:%Y-%m-%d %H}Z | día objetivo local {vent['dia_objetivo']}")
    if not vent["cubre_dia_objetivo"]:
        logger.warning(f"El horizonte no llega al fin del día objetivo ({vent['objetivo_fin']:%Y-%m-%d %H}Z): "
                       "el producto cubrirá solo parte del día.")
    if datetime.now(timezone.utc).replace(tzinfo=None) < t0:
        logger.warning(f"t0 ({t0:%Y-%m-%d %H}Z) todavía no ocurrió: la asimilación usará menos "
                       "observaciones de las previstas.")

    caso_name = f"{fecha}_{ciclo}z"
    caso_dir = OUT_BASE / caso_name
    nudged_dir = caso_dir / "nudged"
    input_dir = caso_dir / "input"
    productos_dir = caso_dir / "productos"
    caso_dir.mkdir(parents=True, exist_ok=True)

    estado: Dict[str, Any] = {
        "fecha": fecha, "ciclo": ciclo, "run_hours": run_hours,
        "t0": t0.strftime("%Y-%m-%d %H:%M"), "asim_hours": vent["asim_hours"],
        "horizon_h": vent["horizon_h"], "dia_objetivo_local": str(vent["dia_objetivo"]),
        "estado": "EJECUTANDO", "detalle": "", "obs_nudge_opt": 1,
        "fuente_gfs": "-", "obs_dia": 0, "obs_ventana": 0, "obs_estaciones": 0,
        "obs_ventana_asim": 0, "obs_estaciones_asim": 0, "holdout": bool(args.holdout),
        "n_wrfout": 0, "ts_inicio": datetime.now().isoformat(timespec="seconds"),
    }

    # ---- 1. Observaciones ----
    try:
        csv = obtener_obs_ventana(start_dt, t0, args.force)
        asimilables = _estaciones_asimilables() if args.holdout else None
        cov = cobertura_obs(csv, start_dt, t0, asimilables=asimilables)
        estado.update(obs_dia=cov["dia"], obs_ventana=cov["ventana"], obs_estaciones=cov["estaciones"],
                      obs_ventana_asim=cov["ventana_asim"], obs_estaciones_asim=cov["estaciones_asim"])
    except Exception as e:
        logger.exception("Fallo al obtener observaciones")
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": f"obs: {e}"})
        return 1

    if args.dry_run:
        gfs = verificar_gfs_completo(fecha, ciclo, run_hours)
        logger.info(f"[DRY] {fecha} {ciclo}Z (t0 {t0:%H}Z, total {run_hours} h): "
                    f"obs csv={cov['dia']} ventana={cov['ventana']} reg / {cov['estaciones']} est "
                    f"(asim {cov['ventana_asim']} reg / {cov['estaciones_asim']} est) | "
                    f"GFS {('completo (' + str(gfs['fuente']) + ')') if gfs['completo'] else 'INCOMPLETO: faltan f' + str(gfs['faltantes'])}")
        previo = {}
        try:
            previo = json.loads((caso_dir / "estado.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        sufijo = "dry_run" if previo.get("estado") == "EJECUTADO" else ""
        escribir_informe(caso_dir, {**estado, "estado": "DRY_RUN",
                                    "detalle": f"GFS {'ok' if gfs['completo'] else 'incompleto'}"},
                         sufijo=sufijo)
        return 0

    # Los umbrales se evalúan sobre las estaciones que realmente entran a OBS_DOMAIN101:
    # con --holdout se excluyen las de rol=evaluacion; sin él se asimilan todas.
    if (cov["ventana_asim"] < OBS_MIN_REGISTROS or cov["estaciones_asim"] < OBS_MIN_ESTACIONES) \
            and not args.continue_sin_obs:
        det = (f"cobertura insuficiente en la ventana de asimilación ({cov['ventana_asim']} registros, "
               f"{cov['estaciones_asim']} estaciones asimilables; holdout={bool(args.holdout)}). "
               "Usá --continue-sin-obs para pronóstico GFS puro.")
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "SIN_OBS", "detalle": det})
        return 3

    obs_nudge_opt = 1 if cov["estaciones_asim"] > 0 else 0
    if args.continue_sin_obs and obs_nudge_opt == 0:
        logger.warning("Sin observaciones: corrida degradada a pronóstico GFS puro (obs_nudge_opt=0).")
    estado["obs_nudge_opt"] = obs_nudge_opt

    # ---- 2. QC + Little_R + OBS_DOMAIN101 ----
    try:
        df = generar_dataframe_validado(csv, start_dt, t0)
        if df.empty:
            det = (f"sin observaciones válidas dentro de la ventana de asimilación "
                   f"[{start_dt:%Y-%m-%d %H:%M}, {t0:%Y-%m-%d %H:%M}] UTC.")
            logger.error(det)
            escribir_informe(caso_dir, {**estado, "estado": "SIN_OBS", "detalle": det})
            return 3
        obs_flat = generar_obs_flat(df)
        obs_flat_path = caso_dir / "input" / "obs_flat.json"
        obs_flat_path.parent.mkdir(parents=True, exist_ok=True)
        obs_flat_path.write_text(json.dumps(obs_flat, indent=1), encoding="utf-8")
        obsdomain, num_obs = generar_obsdomain(df, input_dir, holdout=bool(args.holdout))
    except Exception as e:
        logger.exception("Fallo en QC/Little_R/OBS_DOMAIN101")
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": f"obsdomain: {e}"})
        return 1

    if num_obs == 0:
        if not args.continue_sin_obs:
            det = "OBS_DOMAIN101 sin observaciones (revisar el QC de las estaciones con datos)."
            logger.error(det)
            escribir_informe(caso_dir, {**estado, "estado": "SIN_OBS", "detalle": det})
            return 3
        logger.warning("OBS_DOMAIN101 vacío: corrida degradada a pronóstico GFS puro (obs_nudge_opt=0).")
        obs_nudge_opt = 0
        estado["obs_nudge_opt"] = 0

    # ---- 3. GFS ----
    gfs_state = verificar_gfs_completo(fecha, ciclo, run_hours)
    if not gfs_state["completo"]:
        det = f"GFS {ciclo}Z incompleto en {fecha}: faltan f{'/f'.join(str(x) for x in gfs_state['faltantes'])}"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "SIN_GFS", "detalle": det})
        return 3
    estado["fuente_gfs"] = gfs_state["fuente"] or "aws"
    if not args.skip_wrf:
        n_gfs = descargar_gfs(fecha, ciclo, run_hours, fuente=estado["fuente_gfs"])
        esperados = len(gfs_state["fhoras"])
        if n_gfs < esperados:
            det = f"se descargaron {n_gfs}/{esperados} ficheros GFS"
            logger.error(det)
            escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": f"gfs: {det}"})
            return 1

    if args.skip_wrf:
        estado["estado"] = "PREPARADO"
        estado["detalle"] = "material listo en input/ (--skip-wrf)"
        escribir_informe(caso_dir, estado)
        logger.info(f"Modo --skip-wrf: fin. Material en {caso_dir}")
        return 0

    # ---- 4. WPS ----
    ok_wps, log_wps = ejecutar_wps(fecha, ciclo, run_hours, timeout=args.wps_timeout)
    if not ok_wps:
        det = f"WPS falló (ver {log_wps})"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": det})
        return 1
    n_metem = propagar_metem(fecha, ciclo, run_hours)
    intervalo_s = _leer_namelist_valor(NAMLIST_BASE, "interval_seconds") or 10800
    esperados = int(round(run_hours * 3600 / intervalo_s)) + 1
    if n_metem < esperados:
        det = f"met_em incompletos: {n_metem}/{esperados} archivos propagados al run dir"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": det})
        return 1
    if not KEEP_GFS:
        gfs_dir = WPS_DIR / "gfs_data" / f"{fecha}_{ciclo}"
        if gfs_dir.exists():
            shutil.rmtree(gfs_dir, ignore_errors=True)
            logger.info(f"GFS purgado tras WPS: {gfs_dir}")

    # ---- 5. Verificar run dir + namelist ----
    if not check_run_dir(RUN_DIR):
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": "run dir WRF no disponible"})
        return 1
    coefs = leer_coefs_base(NAMLIST_BASE, args)
    namelist_diario = preparar_namelist(NAMLIST_BASE, obs_nudge_opt, input_dir / "namelist_diario.input",
                                        start_dt, run_hours, coefs, asim_hours=vent["asim_hours"])
    copiar_archivo(obsdomain, RUN_DIR / "OBS_DOMAIN101")
    copiar_archivo(namelist_diario, RUN_DIR / "namelist.input")

    # ---- 6. Preflight ----
    from src.preflight.preflight import correr_preflight_observaciones

    informe_preflight = correr_preflight_observaciones(
        obs_json=obs_flat_path, obsdomain=Path(obsdomain), namelist=namelist_diario,
        start_dt=start_dt, caso="diario", config_path=ROOT / "config" / "preflight_config.json",
        t0=t0,
    )
    logger.info("=== PREFLIGHT ===")
    for line in informe_preflight.resumen_log().splitlines():
        logger.info(f"  {line}")
    informe_preflight.guardar(caso_dir / "preflight_diario.json")
    if not informe_preflight.puede_ejecutar:
        det = "PREFLIGHT BLOQUEANTE: no se ejecuta wrf.exe. Corregir chequeos."
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "BLOQUEADO", "detalle": det})
        return 1

    # ---- 7. real.exe + wrf.exe ----
    if not ejecutar_real(RUN_DIR, args.wrf_timeout):
        det = "real.exe falló"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": det})
        return 1
    if not ejecutar_wrf(RUN_DIR, args.wrf_timeout, reintentos=args.reintentos):
        det = "wrf.exe nudged falló"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": det})
        return 1
    n_wrfout = copiar_wrfout(RUN_DIR, nudged_dir)
    if n_wrfout == 0:
        det = "No se encontraron wrfout tras la corrida"
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": det})
        return 1
    estado["n_wrfout"] = n_wrfout

    # ---- 8. Producto de pronóstico ----
    # CSV con los valores del pronóstico; cada fila va marcada como asimilación o pronóstico libre.
    rc_v, det_v = extraer_valores(nudged_dir, caso_dir, t0)
    if rc_v != 0:
        logger.warning(f"Extracción de valores con errores: {det_v}")

    if not args.skip_products:
        # Mapas del día objetivo (día local siguiente al de t0), cada 3 h, dentro del pronóstico libre.
        valid_times = vent["valid_times"]
        rc, det = generar_producto(nudged_dir, productos_dir, None, valid_times)
        if rc != 0:
            logger.warning(f"Generación de producto con errores parciales: {det}")

    estado["estado"] = "EJECUTADO"
    estado["detalle"] = (f"asimilación {vent['asim_hours']} h hasta t0 + pronóstico libre "
                         f"{vent['horizon_h']} h (obs_nudge_opt={obs_nudge_opt})")
    estado["ts_fin"] = datetime.now().isoformat(timespec="seconds")
    escribir_informe(caso_dir, estado)
    logger.info(f"Pronóstico diario completo: {caso_dir}")
    return 0


def copiar_archivo(src: Path, dst: Path) -> bool:
    src, dst = Path(src), Path(dst)
    if not src.exists():
        logger.error(f"No existe: {src}")
        return False
    shutil.copy2(src, dst)
    logger.info(f"  Copiado {src.name} -> {dst}")
    return True


def main():
    p = argparse.ArgumentParser(description="Pronóstico diario WRF con obs nudging (v1 manual).",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fecha", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   help="Fecha del ciclo (default: hoy UTC).")
    p.add_argument("--ciclo", default=CICLO_DEFAULT,
                   help=f"Ciclo GFS UTC en que arranca el modelo (default: {CICLO_DEFAULT}).")
    p.add_argument("--asim-hours", type=int, default=ASIM_HOURS_DEFAULT,
                   help=f"Horas de asimilación desde el inicio; t0 = inicio + asim-hours (default: {ASIM_HOURS_DEFAULT}).")
    p.add_argument("--horizon-h", type=int, default=None,
                   help="Horas de pronóstico libre desde t0 (default: las que hagan falta para "
                        "cubrir el día objetivo).")
    p.add_argument("--dia-objetivo", default=None,
                   help="Día objetivo local YYYY-MM-DD (default: día local siguiente al de t0). "
                        "Con los 4 ciclos se usa la fecha del ciclo + 1 día.")
    p.add_argument("--holdout", action="store_true",
                   help="No asimilar estaciones con rol=evaluacion (verificación independiente).")
    p.add_argument("--run-hours", type=int, default=None,
                   help="Duración total (asimilación + libre). Alternativa a --horizon-h.")
    p.add_argument("--dry-run", action="store_true", help="Solo disponibilidad (obs + GFS), sin ejecutar nada pesado.")
    p.add_argument("--skip-wrf", action="store_true", help="Preparar material hasta OBS_DOMAIN101 sin WRF.")
    p.add_argument("--skip-products", action="store_true", help="No generar mapas del producto de pronóstico.")
    p.add_argument("--continue-sin-obs", action="store_true",
                   help="Si faltan obs, correr igual el pronóstico (GFS puro, degradado).")
    p.add_argument("--force", action="store_true", help="Re-descargar observaciones aunque exista el CSV.")
    p.add_argument("--obs-coef-wind", type=float, default=None)
    p.add_argument("--obs-coef-temp", type=float, default=None)
    p.add_argument("--obs-coef-mois", type=float, default=None)
    p.add_argument("--obs-twindo", type=float, default=None)
    p.add_argument("--wrf-timeout", type=int, default=14400, help="Timeout de real/wrf.exe en segundos (default 4h).")
    p.add_argument("--wps-timeout", type=int, default=7200, help="Timeout para WPS en segundos.")
    p.add_argument("--reintentos", type=int, default=5, help="Reintentos de wrf.exe si aborta (SIGSEGV intermitente).")
    args = p.parse_args()
    try:
        calcular_ventana(args.fecha, args.ciclo, args.asim_hours, args.horizon_h, args.run_hours,
                         dia_objetivo=args.dia_objetivo)
    except ValueError as e:
        p.error(str(e))
    return pipeline(args)


if __name__ == "__main__":
    sys.exit(main())