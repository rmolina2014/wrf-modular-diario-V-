"""Chequeo posterior a la corrida: ¿el obs nudging dejó de actuar en ``fdda_end``?

El pipeline apaga el nudging en t0 con ``fdda_end = asim_hours * 60`` (minutos desde el inicio
del modelo). Este módulo lo comprueba en el log de ``wrf.exe``: mientras el nudging está activo,
WRF imprime en cada paso ``CALL IN4DOB AT KTAU = n AND XTIME = x: NSTA = m`` (requiere
``obs_ipf_in4dob = .true.`` en el namelist). Si la última llamada ocurre después de ``fdda_end``
(más una tolerancia de un paso de tiempo), el nudging siguió actuando en el pronóstico libre.

Es evidencia indirecta (se basa en lo que WRF imprime, no en las tendencias aplicadas). La prueba
definitiva es reiniciar desde un ``wrfrst`` en t0 con ``obs_nudge_opt = 0`` y comparar.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

_PATRON = re.compile(r"CALL IN4DOB AT KTAU =\s*(\d+) AND XTIME =\s*([\d.]+):\s+NSTA =\s*(\d+)")

# max_time_step = 120 s: el último paso con nudging puede caer hasta 2 min después de fdda_end.
TOLERANCIA_MIN = 2.0

OK = "OK"
VIOLADO = "VIOLADO"
SIN_NUDGING = "SIN_NUDGING"
SIN_DIAGNOSTICO = "SIN_DIAGNOSTICO"
SIN_LOG = "SIN_LOG"


def analizar_log(log_path: Union[str, Path], fdda_end_min: float, esperaba_nudging: bool = True,
                 tolerancia_min: float = TOLERANCIA_MIN) -> Dict[str, Any]:
    """Analiza ``wrf_nudged.log`` y devuelve el estado del apagado del nudging.

    Estados: ``OK`` (la última llamada a IN4DOB no supera ``fdda_end`` + tolerancia),
    ``VIOLADO`` (la supera), ``SIN_NUDGING`` (no hay llamadas y no se esperaba nudging),
    ``SIN_DIAGNOSTICO`` (se esperaba nudging pero el log no trae las líneas: no se puede
    comprobar; revisar ``obs_ipf_in4dob``) y ``SIN_LOG`` (no existe el log).
    """
    ruta = Path(log_path)
    res: Dict[str, Any] = {"estado": SIN_LOG, "fdda_end_min": float(fdda_end_min),
                           "ultimo_xtime_min": None, "pasos_con_nudging": 0,
                           "nsta_en_el_corte": None, "detalle": ""}
    if not ruta.exists():
        res["detalle"] = f"No existe el log de wrf.exe: {ruta}"
        return res

    pasos = set()
    ultimo_x: Optional[float] = None
    ultimo_n: Optional[int] = None
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            m = _PATRON.search(linea)
            if not m:
                continue
            pasos.add(int(m.group(1)))
            x = float(m.group(2))
            if ultimo_x is None or x >= ultimo_x:
                ultimo_x, ultimo_n = x, int(m.group(3))

    res["pasos_con_nudging"] = len(pasos)
    if ultimo_x is None:
        if esperaba_nudging:
            res["estado"] = SIN_DIAGNOSTICO
            res["detalle"] = ("El log no trae llamadas a IN4DOB aunque obs_nudge_opt=1: no se puede "
                              "comprobar el apagado (¿obs_ipf_in4dob = .false.?).")
        else:
            res["estado"] = SIN_NUDGING
            res["detalle"] = "Sin llamadas a IN4DOB (corrida sin nudging)."
        return res

    res["ultimo_xtime_min"] = ultimo_x
    res["nsta_en_el_corte"] = ultimo_n
    exceso = ultimo_x - float(fdda_end_min)
    if exceso > tolerancia_min:
        res["estado"] = VIOLADO
        res["detalle"] = (f"El nudging siguió actuando {exceso:.1f} min después de fdda_end="
                          f"{float(fdda_end_min):.0f} min (última llamada a IN4DOB en XTIME={ultimo_x:.2f} min).")
    else:
        res["estado"] = OK
        res["detalle"] = (f"Último paso con nudging en XTIME={ultimo_x:.2f} min (fdda_end="
                          f"{float(fdda_end_min):.0f} min); {ultimo_n} obs vigentes en el corte.")
    return res
