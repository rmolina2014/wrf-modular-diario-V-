#!/usr/bin/env python3
"""Comprueba en el log de una corrida que el obs nudging se apagó en fdda_end.

Uso:
    python scripts/verificar_nudging.py --caso outputs/2026-10-22_06z
    python scripts/verificar_nudging.py --log ruta/wrf_nudged.log --fdda-end 720

Sin ``--fdda-end`` se lee del namelist de la corrida (``<caso>/input/namelist_diario.input``).
Código de salida: 0 = OK / sin nudging, 1 = nudging posterior a fdda_end, 2 = no se pudo comprobar.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.modelo import nudging_check  # noqa: E402


def _fdda_end_de_namelist(path: Path):
    try:
        m = re.search(r"^\s*fdda_end\s*=\s*([\d.]+)", path.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        return None
    return float(m.group(1)) if m else None


def _nudging_esperado(path: Path) -> bool:
    try:
        m = re.search(r"^\s*obs_nudge_opt\s*=\s*(\d+)", path.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        return True
    return not m or int(m.group(1)) != 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--caso", help="Carpeta de la corrida (outputs/YYYY-MM-DD_HHz).")
    p.add_argument("--log", help="Log de wrf.exe (default: <caso>/logs/wrf_nudged.log).")
    p.add_argument("--fdda-end", type=float, help="fdda_end en minutos (default: el del namelist del caso).")
    p.add_argument("--guardar", action="store_true",
                   help="Escribe el resultado en <caso>/estado.json (claves nudging_*).")
    args = p.parse_args()

    caso = Path(args.caso) if args.caso else None
    log = Path(args.log) if args.log else (caso / "logs" / "wrf_nudged.log" if caso else None)
    if log is None:
        p.error("indicá --caso o --log")
    namelist = caso / "input" / "namelist_diario.input" if caso else None
    fdda_end = args.fdda_end
    if fdda_end is None and namelist is not None:
        fdda_end = _fdda_end_de_namelist(namelist)
    if fdda_end is None:
        print("No se pudo determinar fdda_end: usá --fdda-end.", file=sys.stderr)
        return 2
    esperado = _nudging_esperado(namelist) if namelist is not None else True

    res = nudging_check.analizar_log(log, fdda_end, esperaba_nudging=esperado)
    print(json.dumps(res, indent=2, ensure_ascii=False))

    if args.guardar and caso is not None:
        ruta = caso / "estado.json"
        try:
            estado = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            estado = {}
        estado.update(nudging_estado=res["estado"], nudging_ultimo_xtime_min=res["ultimo_xtime_min"],
                      nudging_fdda_end_min=res["fdda_end_min"])
        ruta.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")

    if res["estado"] in (nudging_check.OK, nudging_check.SIN_NUDGING):
        return 0
    return 1 if res["estado"] == nudging_check.VIOLADO else 2


if __name__ == "__main__":
    sys.exit(main())
