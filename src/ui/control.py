"""Control de las corridas desde la interfaz: armar el comando, lanzarlo y ver su estado.

El comando se arma solo con opciones validadas (nunca texto libre) y se lanza como proceso
desacoplado (``start_new_session``): la corrida dura ~25 min y no debe morir si se cierra
la pestaña o la app.
"""

from __future__ import annotations

import errno
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CICLOS = ("00", "06", "12", "18")
ASIM_MIN, ASIM_MAX = 3, 24
LOCK_OBSOLETO_H = 6.0  # mismo criterio que BloqueoCorrida en pipeline_diario.py


def construir_comando(root: Path, fecha: str, ciclo: str, asim_hours: int = 12,
                      holdout: bool = False, dry_run: bool = False,
                      continue_sin_obs: bool = False) -> List[str]:
    """Comando de ``run_diario.sh`` con los parámetros validados. Lanza ValueError si son inválidos."""
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError(f"Fecha inválida (se espera YYYY-MM-DD): {fecha!r}")
    ciclo = str(ciclo).zfill(2)
    if ciclo not in CICLOS:
        raise ValueError(f"Ciclo inválido: {ciclo!r} (usar {', '.join(CICLOS)})")
    if isinstance(asim_hours, bool) or not isinstance(asim_hours, int) or not ASIM_MIN <= asim_hours <= ASIM_MAX:
        raise ValueError(f"Horas de asimilación fuera de rango ({ASIM_MIN}-{ASIM_MAX}): {asim_hours!r}")

    cmd = ["bash", str(Path(root) / "run_diario.sh"),
           "--fecha", fecha, "--ciclo", ciclo, "--asim-hours", str(asim_hours)]
    if holdout:
        cmd.append("--holdout")
    if dry_run:
        cmd.append("--dry-run")
    if continue_sin_obs:
        cmd.append("--continue-sin-obs")
    return cmd


def estado_corrida(root: Path) -> Dict[str, Any]:
    """¿Hay una corrida en curso? Lee ``logs/corrida.lock`` igual que el pipeline."""
    lock = Path(root) / "logs" / "corrida.lock"
    try:
        contenido = lock.read_text(encoding="utf-8").split()
        edad_h = (time.time() - lock.stat().st_mtime) / 3600.0
        pid = int(contenido[0])
    except (OSError, ValueError, IndexError):
        return {"en_curso": False, "pid": None, "desde": None}
    if edad_h > LOCK_OBSOLETO_H:
        return {"en_curso": False, "pid": pid, "desde": None}
    try:
        os.kill(pid, 0)
        vivo = True
    except OverflowError:
        vivo = False  # PID imposible: el lock está dañado
    except OSError as e:
        vivo = e.errno == errno.EPERM  # EPERM: existe pero es de otro usuario
    return {"en_curso": vivo, "pid": pid, "desde": contenido[1] if len(contenido) > 1 else None}


def lanzar(root: Path, cmd: List[str]) -> Dict[str, Any]:
    """Lanza ``cmd`` desacoplado de la app. La salida inicial va a ``logs/ui_lanzamiento_*.out``."""
    root = Path(root)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    salida = logs / f"ui_lanzamiento_{datetime.now():%Y%m%d_%H%M%S}.out"
    with open(salida, "wb") as f:
        proc = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=f,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return {"pid": proc.pid, "salida": salida}


def ultimo_log(root: Path, n: int = 60) -> Tuple[Optional[Path], List[str]]:
    """Últimas ``n`` líneas del log de corrida más reciente (``logs/run_*.log``)."""
    logs = Path(root) / "logs"
    candidatos = sorted(logs.glob("run_*.log"), key=lambda p: p.stat().st_mtime) if logs.is_dir() else []
    if not candidatos:
        return None, []
    ruta = candidatos[-1]
    try:
        lineas = ruta.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ruta, []
    return ruta, lineas[-n:]
