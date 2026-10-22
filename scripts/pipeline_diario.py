#!/usr/bin/env python3
"""Pronóstico diario WRF con asimilación de observaciones EcoWitt (obs nudging).

v1: ejecución manual por el operador (la automatización systemd es etapa 2).

Flujo del día:
  1. Observaciones EcoWitt históricas del día  -> cobertura -> CSV/JSON
  2. QC + Little_R + OBS_DOMAIN101
  3. GFS <ciclo>Z (0..run_hours) -> WPS -> met_em -> run dir WRF
  4. namelist diario (start=<fecha> <ciclo>Z, run_hours, obs_nudge_opt=1)
  5. Preflight (bloqueante)
  6. real.exe -> wrf.exe nudged (reintentos)
  7. Producto de pronóstico (mapas T2/RH/PSFC/viento) + informe diario

Salidas: outputs/<fecha>_<ciclo>z/  {input, nudged, productos}
Logs:    logs/run_<fecha>_<ciclo>z.log

Uso:
    ./run_diario.sh                              # corrida de hoy, ciclo 12Z, 36h
    ./run_diario.sh --fecha 2026-10-13           # otra fecha
    ./run_diario.sh --dry-run                    # solo disponibilidad (obs + GFS)
    ./run_diario.sh --continue-sin-obs           # pronóstico GFS puro si faltan obs
    ./run_diario.sh --skip-wrf                   # solo preparar material hasta OBS_DOMAIN101
"""

import argparse
import errno
import json
import logging
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

# --- Umbrales de cobertura observacional ---
OBS_MIN_REGISTROS = int(os.environ.get("OBS_MIN_REGISTROS", "60"))
OBS_MIN_ESTACIONES = int(os.environ.get("OBS_MIN_ESTACIONES", "3"))

GFS_URLS = {
    "aws": "https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{fecha}/{ciclo}/atmos/{file}",
    "nomads": "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{fecha}/{ciclo}/atmos/{file}",
}


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
# Observaciones EcoWitt
# --------------------------------------------------------------------------
def descargar_obs(fecha: str, fuerza: bool = False) -> Path:
    """Descarga el histórico diario si no existe y devuelve la ruta del CSV."""
    css = DATA_RAW / f"ecowitt_historico_{fecha.replace('-', '')}.csv"
    if not fuerza and css.exists() and css.stat().st_size > 100:
        logger.info(f"Histórico ya descargado: {css.name}")
        return css
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


def _estaciones_asimilables() -> Optional[set]:
    """Nombres de estaciones con rol != 'evaluacion' (las que sí entran a OBS_DOMAIN101).

    Devuelve None si no se puede leer el catálogo, porque entonces no se puede
    discriminar el hold-out y se cuentan todas las estaciones.
    """
    try:
        catalogo = json.loads(ESTACIONES_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"No se pudo leer {ESTACIONES_JSON.name} para el hold-out: {e}")
        return None
    return {n for n, meta in catalogo.items() if (meta or {}).get("rol") != "evaluacion"}


def cobertura_obs(csv_path: Path, fecha: str, ciclo: str = "12") -> Dict[str, Any]:
    """Mide la cobertura de observaciones válidas (temperatura) del día y en la ventana.

    Devuelve métricas del día completo, de la ventana del ciclo (ciclo-12h .. ciclo+24h)
    y, restringidas a estaciones asimilables (rol != 'evaluacion'), las que se usan para
    decidir si la corrida avanza.
    """
    df = pd.read_csv(csv_path)
    vacio = {"dia": 0, "ventana": 0, "estaciones": 0, "por_estacion": {},
             "ventana_asim": 0, "estaciones_asim": 0}
    if df.empty or "temp_c" not in df.columns:
        return vacio

    # La ventana del ciclo se interpreta en UTC (WRF trabaja en UTC).
    ts_ciclo = int(datetime.strptime(f"{fecha} {ciclo}:00:00", "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())
    ts_ini = ts_ciclo - 12 * 3600
    ts_fin = ts_ciclo + 24 * 3600
    df["ts"] = pd.to_numeric(df["hora_unix"], errors="coerce")
    df["temp_ok"] = df["temp_c"].apply(lambda v: _num(v) is not None)

    en_ventana = df[df["temp_ok"] & df["ts"].between(ts_ini, ts_fin)]
    dia = int(df["temp_ok"].sum())
    ventana = int(len(en_ventana))
    por_estacion = en_ventana.groupby("estacion")["temp_ok"].sum().astype(int).to_dict()

    asimilables = _estaciones_asimilables()
    asim = en_ventana if asimilables is None else en_ventana[en_ventana["estacion"].isin(asimilables)]
    por_estacion_asim = asim.groupby("estacion")["temp_ok"].sum().astype(int).to_dict()

    return {"dia": dia, "ventana": ventana, "estaciones": len(por_estacion), "por_estacion": por_estacion,
            "ventana_asim": int(len(asim)), "estaciones_asim": len(por_estacion_asim),
            "por_estacion_asim": por_estacion_asim}


def generar_dataframe_validado(csv_path: Path) -> pd.DataFrame:
    """Aplica QC al CSV histórico y devuelve el DataFrame normalizado."""
    from src.calidad.cleaner import ObservacionesCleaner

    cleaner = ObservacionesCleaner(str(DATA_RAW))
    df = cleaner.procesar_csv_historico(str(csv_path))
    logger.info(f"QC: {len(df)} registros válidos tras la limpieza.")
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
    """Verifica disponibilidad del archivo pgrb2 0.25 en AWS/NOMADS. Devuelve fuente o None."""
    fstr = f"{fhour:03d}"
    file = f"gfs.t{ciclo}z.pgrb2.0p25.f{fstr}"
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
    file = f"gfs.t{ciclo}z.pgrb2.0p25.f{fstr}"
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
        fname = f"gfs.t{ciclo}z.pgrb2.0p25.f{fh:03d}"
        p = out / fname
        if not p.exists():
            continue
        esperado = _tamano_esperado_gfs(fecha, ciclo, fh, fuente)
        if esperado and abs(p.stat().st_size - esperado) > 0.02 * esperado:
            logger.warning(f"GFS corrupto ({p.stat().st_size} vs {esperado} B): re-descargando {fname}")
            p.unlink()
    GFSDownloader(str(out)).descargar_ciclo(fecha, ciclo_hora=ciclo, horas_pronostico=horas, fuente=fuente)
    return sum(1 for fh in horas if (out / f"gfs.t{ciclo}z.pgrb2.0p25.f{fh:03d}").exists())


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
                      start_dt: datetime, run_hours: int, coefs: Dict[str, float]) -> Path:
    from src.modelo.namelist_manager import NamelistManager

    mgr = NamelistManager(template_path=str(namelist_src))
    end_dt = start_dt + timedelta(hours=run_hours)
    mgr.actualizar_fechas(
        start_year=start_dt.year, start_month=start_dt.month, start_day=start_dt.day,
        start_hour=start_dt.hour,
        end_year=end_dt.year, end_month=end_dt.month, end_day=end_dt.day, end_hour=end_dt.hour,
        run_hours=run_hours,
    )
    # fdda_end = duración del ciclo en minutos (nudging durante todo el pronóstico)
    mgr.contenido = re.sub(r"fdda_end\s*=\s*\d+", f"fdda_end = {run_hours * 60}", mgr.contenido)
    mgr.configurar_fdda(obs_nudge_opt=obs_nudge_opt, **coefs)
    mgr.guardar(str(output_path))
    logger.info(f"Namelist diario: {output_path.name} (start {start_dt}, run_hours={run_hours}, "
                f"obs_nudge_opt={obs_nudge_opt}, fdda_end={run_hours * 60} min)")
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


def generar_obsdomain(df: pd.DataFrame, out_dir: Path) -> Tuple[Path, int]:
    from src.asimilacion.littler_writer import LittleRWriter
    from src.asimilacion.obsnud_writer import ObsNudWriter

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = df["fecha"].iloc[0].replace("-", "") + "_" + str(df["hora"].iloc[0]).replace(":", "")
    littler_path, num_littler = LittleRWriter(config_estaciones_path=str(ESTACIONES_JSON)).generar_littler(
        df, output_path=str(out_dir / f"littler_{ts}.txt")
    )
    obsdomain = ObsNudWriter(config_estaciones_path=str(ESTACIONES_JSON)).generar_desde_dataframe(
        df, output_path=str(out_dir / "OBS_DOMAIN101")
    )
    # num_obs debe medir el archivo que se asimila (OBS_DOMAIN101 aplica el hold-out
    # rol='evaluacion'), no el littler (que incluye todas las estaciones).
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
           "--output", str(productos_dir)]
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


def escribir_informe(caso_dir: Path, estado: Dict[str, Any]):
    md = caso_dir / "informe_diario.md"
    lines = [f"# Pronóstico diario WRF - {estado['fecha']} {estado['ciclo']}Z",
             "",
             f"**Generado:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (local)",
             f"**Estado:** {estado['estado']}",
             f"**Detalle:** {estado.get('detalle', '')}",
             "",
             "## Configuración",
             "| Parámetro | Valor |",
             "|-----------|-------|",
             f"| Inicio | {estado['fecha']} {estado['ciclo']}Z |",
             f"| Duración | {estado.get('run_hours', 36)} h |",
             f"| Obs nudging | obs_nudge_opt={estado.get('obs_nudge_opt', 1)} |",
             f"| Fuente GFS | {estado.get('fuente_gfs', '-')} |",
             f"| wrfout | {estado.get('n_wrfout', 0)} |",
             "",
             "## Observaciones",
             "",
             f"- Registros válidos del día: **{estado.get('obs_dia', 0)}**",
             f"- En ventana (ciclo±12h): **{estado.get('obs_ventana', 0)}**",
             f"- Estaciones en ventana: **{estado.get('obs_estaciones', 0)}**",
             f"- Asimilables en ventana (rol=asimilacion): **{estado.get('obs_ventana_asim', 0)}** registros, "
             f"**{estado.get('obs_estaciones_asim', 0)}** estaciones",
             "",
             f"## Resultados",
             f"`{caso_dir}`",
             ""]
    md.write_text("\n".join(lines), encoding="utf-8")

    with open(caso_dir / "estado.json", "w", encoding="utf-8") as f:
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
    run_hours = int(args.run_hours)
    start_dt = datetime.strptime(f"{fecha} {ciclo}:00", "%Y-%m-%d %H:%M")

    caso_name = f"{fecha}_{ciclo}z"
    caso_dir = OUT_BASE / caso_name
    nudged_dir = caso_dir / "nudged"
    input_dir = caso_dir / "input"
    productos_dir = caso_dir / "productos"
    caso_dir.mkdir(parents=True, exist_ok=True)

    estado: Dict[str, Any] = {
        "fecha": fecha, "ciclo": ciclo, "run_hours": run_hours,
        "estado": "EJECUTANDO", "detalle": "", "obs_nudge_opt": 1,
        "fuente_gfs": "-", "obs_dia": 0, "obs_ventana": 0, "obs_estaciones": 0,
        "obs_ventana_asim": 0, "obs_estaciones_asim": 0,
        "n_wrfout": 0, "ts_inicio": datetime.now().isoformat(timespec="seconds"),
    }

    # ---- 1. Observaciones ----
    try:
        csv = descargar_obs(fecha, args.force)
        cov = cobertura_obs(csv, fecha, ciclo)
        estado.update(obs_dia=cov["dia"], obs_ventana=cov["ventana"], obs_estaciones=cov["estaciones"],
                      obs_ventana_asim=cov["ventana_asim"], obs_estaciones_asim=cov["estaciones_asim"])
    except Exception as e:
        logger.exception("Fallo al obtener observaciones")
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": f"obs: {e}"})
        return 1

    if args.dry_run:
        gfs = verificar_gfs_completo(fecha, ciclo, run_hours)
        logger.info(f"[DRY] {fecha} {ciclo}Z: obs día={cov['dia']} ventana={cov['ventana']} "
                    f"(asim: {cov['ventana_asim']} reg / {cov['estaciones_asim']} est) | "
                    f"GFS {('completo (' + str(gfs['fuente']) + ')') if gfs['completo'] else 'INCOMPLETO: faltan f' + str(gfs['faltantes'])}")
        escribir_informe(caso_dir, {**estado, "estado": "DRY_RUN",
                                    "detalle": f"GFS {'ok' if gfs['completo'] else 'incompleto'}"})
        return 0

    # Los umbrales se evalúan sobre la ventana del ciclo y solo con estaciones
    # asimilables (rol != 'evaluacion'): las de evaluación no entran a OBS_DOMAIN101.
    if (cov["ventana_asim"] < OBS_MIN_REGISTROS or cov["estaciones_asim"] < OBS_MIN_ESTACIONES) \
            and not args.continue_sin_obs:
        det = (f"cobertura asimilable insuficiente en la ventana ({cov['ventana_asim']} registros, "
               f"{cov['estaciones_asim']} estaciones con rol=asimilacion). "
               "Usá --continue-sin-obs para pronóstico GFS puro.")
        logger.error(det)
        escribir_informe(caso_dir, {**estado, "estado": "SIN_OBS", "detalle": det})
        return 3

    obs_nudge_opt = 1 if cov["estaciones_asim"] > 0 else 0
    if args.continue_sin_obs and obs_nudge_opt == 0:
        logger.warning("Sin observaciones asimilables: corrida degradada a pronóstico GFS puro (obs_nudge_opt=0).")
    estado["obs_nudge_opt"] = obs_nudge_opt

    # ---- 2. QC + Little_R + OBS_DOMAIN101 ----
    try:
        df = generar_dataframe_validado(csv)
        obs_flat = generar_obs_flat(df)
        obs_flat_path = caso_dir / "input" / "obs_flat.json"
        obs_flat_path.parent.mkdir(parents=True, exist_ok=True)
        obs_flat_path.write_text(json.dumps(obs_flat, indent=1), encoding="utf-8")
        obsdomain, num_obs = generar_obsdomain(df, input_dir)
    except Exception as e:
        logger.exception("Fallo en QC/Little_R/OBS_DOMAIN101")
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": f"obsdomain: {e}"})
        return 1

    if num_obs == 0:
        if not args.continue_sin_obs:
            det = ("OBS_DOMAIN101 sin observaciones asimilables (revisar QC y hold-out "
                   "rol=evaluacion en config/estaciones.json).")
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

    # ---- 5. Verificar run dir + namelist ----
    if not check_run_dir(RUN_DIR):
        escribir_informe(caso_dir, {**estado, "estado": "FALLIDO", "detalle": "run dir WRF no disponible"})
        return 1
    coefs = leer_coefs_base(NAMLIST_BASE, args)
    namelist_diario = preparar_namelist(NAMLIST_BASE, obs_nudge_opt, input_dir / "namelist_diario.input",
                                        start_dt, run_hours, coefs)
    copiar_archivo(obsdomain, RUN_DIR / "OBS_DOMAIN101")
    copiar_archivo(namelist_diario, RUN_DIR / "namelist.input")

    # ---- 6. Preflight ----
    from src.preflight.preflight import correr_preflight_observaciones

    informe_preflight = correr_preflight_observaciones(
        obs_json=obs_flat_path, obsdomain=Path(obsdomain), namelist=namelist_diario,
        start_dt=start_dt, caso="diario", config_path=ROOT / "config" / "preflight_config.json",
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
    if not args.skip_products:
        # Tiempos de mañana (00Z..24Z del día siguiente): productos del pronóstico
        manana = (start_dt + timedelta(hours=12)).strftime("%Y-%m-%d")
        valid_times = [
            (start_dt + timedelta(hours=h)).strftime("%Y-%m-%d_%H:%M:%S")
            for h in range(12, run_hours + 1, 3)
            if (start_dt + timedelta(hours=h)).strftime("%Y-%m-%d") == manana
        ] or [(start_dt + timedelta(hours=h)).strftime("%Y-%m-%d_%H:%M:%S") for h in range(12, run_hours + 1, 3)]
        rc, det = generar_producto(nudged_dir, productos_dir, None, valid_times)
        if rc != 0:
            logger.warning(f"Generación de producto con errores parciales: {det}")

    estado["estado"] = "EJECUTADO"
    estado["detalle"] = f"pronóstico {run_hours}h con asimilación (obs_nudge_opt={obs_nudge_opt})"
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
                   help="Fecha del ciclo (default: hoy UTC). Con --run-hours 36 el paso del día 12Z cubre mañana.")
    p.add_argument("--ciclo", default="12", help="Ciclo UTC del pronóstico (default: 12).")
    p.add_argument("--run-hours", type=int, default=36, help="Horas de pronóstico (default: 36).")
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
    return pipeline(args)


if __name__ == "__main__":
    sys.exit(main())