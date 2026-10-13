"""Módulo de chequeos previos a la ejecución de WRF (Preflight).

Implementa la Sección 2.2 (observaciones y OBS_DOMAIN101) del documento
`chequeos_previso_ejecucionWRF.md`, sobre un framework extensible para sumar
el resto de las categorías (2.1, 2.3, 2.4, 2.5) sin modificar el runner.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import logging
import re

logger = logging.getLogger("preflight")

CONFIG_DEFAULT = Path(__file__).parent.parent.parent / "config" / "preflight_config.json"


class Estado(Enum):
    OK = "OK"
    ADVERTENCIA = "ADVERTENCIA"
    BLOQUEANTE = "BLOQUEANTE"


@dataclass
class ResultadoChequeo:
    id: str
    descripcion: str
    estado: Estado
    detalle: str = ""


@dataclass
class InformePreflight:
    caso: str
    fecha: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    chequeos: List[ResultadoChequeo] = field(default_factory=list)

    def agregar(self, id: str, descripcion: str, estado: Estado, detalle: str = "") -> None:
        self.chequeos.append(ResultadoChequeo(id, descripcion, estado, detalle))

    @property
    def bloqueantes(self) -> List[ResultadoChequeo]:
        return [c for c in self.chequeos if c.estado == Estado.BLOQUEANTE]

    @property
    def puede_ejecutar(self) -> bool:
        return not self.bloqueantes

    def a_dict(self) -> Dict[str, Any]:
        return {
            "caso": self.caso,
            "fecha": self.fecha,
            "ejecutar_wrf": self.puede_ejecutar,
            "chequeos": [
                {"id": c.id, "descripcion": c.descripcion, "estado": c.estado.value, "detalle": c.detalle}
                for c in self.chequeos
            ],
        }

    def guardar(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.a_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Preflight guardado en {path}")
        return path

    def resumen_log(self) -> str:
        lines = [f"Preflight {self.caso} ({self.fecha}) -> ejecutar_wrf={'SI' if self.puede_ejecutar else 'NO'}"]
        for c in self.chequeos:
            lines.append(f"  [{c.estado.value:>11}] {c.id} {c.descripcion} {(': ' + c.detalle) if c.detalle else ''}")
        n_blk = len(self.bloqueantes)
        n_adv = sum(1 for c in self.chequeos if c.estado == Estado.ADVERTENCIA)
        lines.append(f"  Resumen: {len(self.chequeos)} chequeos, {n_blk} bloqueantes, {n_adv} advertencias.")
        return "\n".join(lines)


# === Utilidades de parseo ===

def _cargar_config(path: Optional[Path] = None) -> Dict[str, Any]:
    src = Path(path or CONFIG_DEFAULT)
    if not src.exists():
        logger.warning(f"Config de preflight no encontrada en {src}; se usan valores default.")
        return {}
    with open(src, "r", encoding="utf-8") as f:
        return json.load(f)


def _leer_namelist_valor(namelist: Path, clave: str) -> Optional[float]:
    """Extrae el valor numérico de una clave del namelist (p. ej. max_obs, run_hours)."""
    if not namelist or not Path(namelist).exists():
        return None
    texto = Path(namelist).read_text(encoding="utf-8")
    m = re.search(rf"^\s*{clave}\s*=\s*([0-9.]+)\s*[,]", texto, re.MULTILINE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _parsear_obsdomain(path: Path) -> List[Dict[str, Any]]:
    """Parsea OBS_DOMAIN101 (FORMAT 105): 5 líneas por observación.

    Línea 1: timestamp YYYYMMDDHHMMSS
    Línea 2: lat lon
    Línea 3: identificador + SURFACE
    Línea 4: plataforma + elevación + flags + contador
    Línea 5: datos (pares valor/qc)
    """
    registros = []
    if not path.exists():
        return registros
    lineas = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    i = 0
    while i + 4 < len(lineas):
        ts = lineas[i].strip()
        partes_latlon = lineas[i + 1].split()
        identificador = lineas[i + 2].strip()
        plataforma = lineas[i + 3]
        datos = lineas[i + 4].split()
        registros.append({
            "timestamp": ts,
            "estacion": identificador,
            "lat": float(partes_latlon[0]) if partes_latlon else None,
            "lon": float(partes_latlon[1]) if len(partes_latlon) > 1 else None,
            "plataforma": plataforma,
            "n_valores": len(datos),
            "n_pares": len(datos) // 2,
        })
        i += 5
    return registros


# === Chequeos de la Sección 2.2 ===

class ChequeosObservaciones:
    """Chequeos 2.2.x sobre el JSON crudo y el OBS_DOMAIN101 generado."""

    def __init__(self, obs_json: Path, obsdomain: Path, namelist: Path,
                 start_dt: Optional[datetime] = None, config: Optional[Dict] = None):
        self.obs_json = Path(obs_json)
        self.obsdomain = Path(obsdomain)
        self.namelist = namelist
        self.start_dt = start_dt
        self.cfg = config or {}

    def _n_registros_json(self) -> int:
        try:
            with open(self.obs_json, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return len(data.get("observaciones", data.get("datos", [])))
        return 0

    # 2.2.1
    def check_2221_archivo_json(self, informe: InformePreflight) -> None:
        if not self.obs_json.exists():
            informe.agregar("2.2.1", "Existencia del JSON de observaciones",
                            Estado.BLOQUEANTE, f"No existe: {self.obs_json}")
            return
        n = self._n_registros_json()
        if n > 0:
            informe.agregar("2.2.1", "Existencia del JSON de observaciones",
                            Estado.OK, f"{n} registros")
        else:
            informe.agregar("2.2.1", "Existencia del JSON de observaciones",
                            Estado.BLOQUEANTE, "Archivo sin registros")

    # 2.2.2
    def check_2222_formato105(self, informe: InformePreflight) -> None:
        if not self.obsdomain.exists():
            informe.agregar("2.2.2", "Formato de superficie (FORMAT 105)",
                            Estado.BLOQUEANTE, f"No existe: {self.obsdomain}")
            return
        regs = _parsear_obsdomain(self.obsdomain)
        pares_esperados = self.cfg.get("formato_obs", {}).get("pares_formato105", 9)
        pares_104 = self.cfg.get("formato_obs", {}).get("pares_formato104", 6)
        if not regs:
            informe.agregar("2.2.2", "Formato de superficie (FORMAT 105)",
                            Estado.BLOQUEANTE, "OBS_DOMAIN101 sin registros parseables")
            return
        pares_por_linea = {r["n_pares"] for r in regs}
        if pares_por_linea == {pares_esperados}:
            informe.agregar("2.2.2", "Formato de superficie (FORMAT 105)",
                            Estado.OK, f"{pares_esperados} pares por línea")
        elif pares_por_linea == {pares_104}:
            informe.agregar("2.2.2", "Formato de superficie (FORMAT 105)",
                            Estado.BLOQUEANTE,
                            f"{pares_104} pares por línea = FORMAT 104 (sondeos), se requiere {pares_esperados}")
        else:
            informe.agregar("2.2.2", "Formato de superficie (FORMAT 105)",
                            Estado.BLOQUEANTE, f"Pares por línea inconsistentes: {pares_por_linea}")

    # 2.2.3
    def check_2223_plataforma(self, informe: InformePreflight) -> None:
        regs = _parsear_obsdomain(self.obsdomain)
        if not regs:
            informe.agregar("2.2.3", "Identificador de plataforma (SYNOP)",
                            Estado.ADVERTENCIA, "Sin registros para evaluar")
            return
        plataforma_esperada = self.cfg.get("formato_obs", {}).get("plataforma_esperada", "SYNOP")
        ok = all(plataforma_esperada in r["plataforma"] for r in regs)
        if ok:
            informe.agregar("2.2.3", "Identificador de plataforma (SYNOP)",
                            Estado.OK, f"'{plataforma_esperada}' presente en {len(regs)} registros")
        else:
            sin_plat = [i for i, r in enumerate(regs) if plataforma_esperada not in r["plataforma"]]
            informe.agregar("2.2.3", "Identificador de plataforma (SYNOP)",
                            Estado.ADVERTENCIA,
                            f"Falta '{plataforma_esperada}' en {len(sin_plat)} registros (p. ej. #{sin_plat[0]}): '{regs[sin_plat[0]]['plataforma'].strip()}'")

    # 2.2.4
    def check_2224_timestamps(self, informe: InformePreflight) -> None:
        regs = _parsear_obsdomain(self.obsdomain)
        if not regs:
            informe.agregar("2.2.4", "Timestamps de las observaciones",
                            Estado.BLOQUEANTE, "Sin registros para evaluar")
            return
        segundos_permitidos = self.cfg.get("formato_obs", {}).get("timestamp_segundos_permitidos", [0])
        problema = None
        timestamps = []
        for r in regs:
            ts = r["timestamp"]
            if not re.fullmatch(r"\d{14}", ts):
                problema = f"Timestamp no estándar (esperado YYYYMMDDHHMMSS): '{ts}'"
                break
            seg = int(ts[-2:])
            if seg not in segundos_permitidos:
                problema = (f"Timestamp con desfase sintético de segundos ({ts[-2:]}s != {segundos_permitidos}); "
                            "el bug _desfase_por_estacion (WRF 4.5 'Bad value during integer read') los genera")
                break
            try:
                timestamps.append(datetime.strptime(ts, "%Y%m%d%H%M%S"))
            except ValueError as e:
                problema = f"Timestamp inválido '{ts}': {e}"
                break
        if problema:
            informe.agregar("2.2.4", "Timestamps de las observaciones",
                            Estado.BLOQUEANTE, problema)
            return
        # Orden estrictamente no decreciente (WRF exige orden temporal)
        desorden = [i for i in range(1, len(timestamps)) if timestamps[i] < timestamps[i - 1]]
        if desorden:
            i = desorden[0]
            informe.agregar("2.2.4", "Timestamps de las observaciones",
                            Estado.BLOQUEANTE,
                            f"Registro #{i} ({timestamps[i]}) anterior al #{i - 1} ({timestamps[i - 1]}); "
                            "WRF aborta con 'in4dob STOP 111'")
            return
        # Cadencia por estación: intervalos entre obs de la misma estación deben
        # respetar el intervalo real de las observaciones (el bug de desfases
        # sintéticos +1s/estación generaba intervalos de 59s dentro del mismo minuto).
        intervalo_min = self.cfg.get("formato_obs", {}).get("intervalo_ts_min", 5)
        tol_s = 1
        estaciones = {}
        for r, ts in zip(regs, timestamps):
            estaciones.setdefault(r["estacion"], []).append(ts)
        intervalos_anomalos = []
        for est, tss in estaciones.items():
            for i in range(1, len(tss)):
                diff_s = (tss[i] - tss[i - 1]).total_seconds()
                if 0 < diff_s < intervalo_min * 60 - tol_s:
                    intervalos_anomalos.append((est, tss[i - 1], tss[i], int(diff_s)))
        if intervalos_anomalos:
            ejemplos = ", ".join(
                f"{est}: {a}->{b} ({d} s)" for est, a, b, d in intervalos_anomalos[:3]
            )
            informe.agregar("2.2.4", "Timestamps de las observaciones",
                            Estado.BLOQUEANTE,
                            f"Intervalos sintéticos (< {intervalo_min} min) en estaciones: {ejemplos}; "
                            "posible _desfase_por_estacion (WRF 4.5 'Bad value during integer read')")
            return
        detalle = f"{len(timestamps)} timestamps válidos, orden cronológico OK, cadencia {intervalo_min} min, segundos en {segundos_permitidos}"
        informe.agregar("2.2.4", "Timestamps de las observaciones", Estado.OK, detalle)

    # 2.2.5
    def check_2225_max_obs(self, informe: InformePreflight) -> None:
        regs = _parsear_obsdomain(self.obsdomain)
        n_obs = len(regs)
        max_obs = _leer_namelist_valor(self.namelist, "max_obs")
        max_obs_min = self.cfg.get("namelist", {}).get("max_obs_min", 1)
        if max_obs is None:
            informe.agregar("2.2.5", "Cantidad de obs vs max_obs",
                            Estado.ADVERTENCIA, "No se pudo leer max_obs del namelist; no se verifica")
            return
        if max_obs <= max_obs_min - 1:
            informe.agregar("2.2.5", "Cantidad de obs vs max_obs",
                            Estado.BLOQUEANTE,
                            f"max_obs={int(max_obs)} <= 0; NIOBF=0 y ninguna observación se lee (bug 12/08/2026)")
            return
        if n_obs > max_obs:
            informe.agregar("2.2.5", "Cantidad de obs vs max_obs",
                            Estado.BLOQUEANTE,
                            f"{n_obs} obs en OBS_DOMAIN101 superan max_obs={int(max_obs)}; se truncarían")
            return
        informe.agregar("2.2.5", "Cantidad de obs vs max_obs",
                        Estado.OK, f"{n_obs} obs <= max_obs={int(max_obs)}")

    # 2.2.6
    def check_2226_cobertura(self, informe: InformePreflight) -> None:
        regs = _parsear_obsdomain(self.obsdomain)
        if not regs or self.start_dt is None:
            informe.agregar("2.2.6", "Cobertura temporal de las observaciones",
                            Estado.ADVERTENCIA, "Start time no disponible para evaluar rebanadas")
            return
        run_hours = _leer_namelist_valor(self.namelist, "run_hours") or 12
        inicio = self.start_dt.replace(minute=0, second=0, microsecond=0)
        fin = inicio + timedelta(hours=run_hours)
        n_tercios = self.cfg.get("cobertura_temporal", {}).get("n_tercios", 3)
        obs_min = self.cfg.get("cobertura_temporal", {}).get("obs_min_por_tercio", 5)
        bordes = []
        for t in range(n_tercios):
            bordes.append(inicio + (fin - inicio) * (t / n_tercios))
        contadores = [0] * n_tercios
        for ts in _parsear_obsdomain(self.obsdomain):
            try:
                dt = datetime.strptime(ts["timestamp"], "%Y%m%d%H%M%S")
            except ValueError:
                continue
            for idx in range(n_tercios):
                if bordes[idx] <= dt < (bordes[idx + 1] if idx + 1 < n_tercios else fin + timedelta(seconds=1)):
                    contadores[idx] += 1
                    break
        bajos = [i + 1 for i, c in enumerate(contadores) if c < obs_min]
        detalle = " / ".join(f"tercio{i + 1}: {c} obs" for i, c in enumerate(contadores))
        if bajos:
            informe.agregar("2.2.6", "Cobertura temporal de las observaciones",
                            Estado.ADVERTENCIA,
                            f"{detalle}; tercios {bajos} con menos de {obs_min} obs (nudging sin efecto ahí)")
        else:
            informe.agregar("2.2.6", "Cobertura temporal de las observaciones",
                            Estado.OK, detalle)


class PreflightRunner:
    """Orquesta los chequeos de secciones activas y devuelve el informe."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config = _cargar_config(config_path)
        self.chequeos: List = []

    def registrar(self, funcion) -> None:
        self.chequeos.append(funcion)

    def correr(self, informe: InformePreflight) -> InformePreflight:
        for check in self.chequeos:
            try:
                check(informe)
            except Exception as e:
                informe.agregar("ERR", getattr(check, "__name__", "check"),
                                Estado.BLOQUEANTE, f"Excepción en chequeo: {e}")
                logger.exception("Fallo en chequeo preflight")
        return informe


def correr_preflight_observaciones(
    obs_json: Path, obsdomain: Path, namelist: Path, start_dt: Optional[datetime] = None,
    caso: str = "", config_path: Optional[Path] = None,
) -> InformePreflight:
    """Ejecuta la Sección 2.2 (observaciones/OBS_DOMAIN101) y devuelve el informe."""
    informe = InformePreflight(caso=caso or str(Path(obsdomain).parent.name))
    chequeos = ChequeosObservaciones(obs_json, obsdomain, namelist, start_dt, _cargar_config(config_path))
    runner = PreflightRunner(config_path=config_path)
    runner.registrar(chequeos.check_2221_archivo_json)
    runner.registrar(chequeos.check_2222_formato105)
    runner.registrar(chequeos.check_2223_plataforma)
    runner.registrar(chequeos.check_2224_timestamps)
    runner.registrar(chequeos.check_2225_max_obs)
    runner.registrar(chequeos.check_2226_cobertura)
    return runner.correr(informe)