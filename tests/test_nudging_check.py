"""Tests del chequeo de apagado del nudging (lectura del log de wrf.exe). No ejecutan WRF."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.modelo import nudging_check as nc  # noqa: E402


def _linea(ktau, xtime, nsta):
    return f"****** CALL IN4DOB AT KTAU = {ktau:5d} AND XTIME = {xtime:10.2f}:  NSTA = {nsta:6d} ******"


def _log(tmp_path, pasos, relleno=True):
    """Log sintético: cada paso imprime IN4DOB varias veces (como WRF con varios procesos/llamadas)."""
    filas = ["Timing for main (dt=120.00): time 2026-10-22_06:02:00 on domain   1:    0.5 elapsed seconds"]
    for k, (x, n) in enumerate(pasos, start=1):
        filas += [_linea(k, x, n)] * 3
        if relleno:
            filas.append("OBS NUDGING: Reading new obs for time window TBACK = 1.0 TFORWD = 3.0 for grid = 1")
    ruta = tmp_path / "wrf_nudged.log"
    ruta.write_text("\n".join(filas) + "\nwrf: SUCCESS COMPLETE WRF\n")
    return ruta


def test_ok_cuando_la_ultima_llamada_es_en_fdda_end(tmp_path):
    log = _log(tmp_path, [(0.0, 90), (360.0, 170), (720.0, 65)])
    r = nc.analizar_log(log, 720)
    assert r["estado"] == nc.OK
    assert r["ultimo_xtime_min"] == 720.0 and r["nsta_en_el_corte"] == 65
    assert r["pasos_con_nudging"] == 3  # se cuentan pasos (KTAU) distintos, no líneas


def test_ok_dentro_de_la_tolerancia_de_un_paso(tmp_path):
    log = _log(tmp_path, [(719.0, 70), (721.9, 65)])  # 1,9 min después: un paso de 120 s
    assert nc.analizar_log(log, 720)["estado"] == nc.OK


def test_violado_si_el_nudging_sigue_despues_de_fdda_end(tmp_path):
    log = _log(tmp_path, [(720.0, 65), (780.0, 40), (900.0, 5)])
    r = nc.analizar_log(log, 720)
    assert r["estado"] == nc.VIOLADO
    assert r["ultimo_xtime_min"] == 900.0
    assert "180.0 min" in r["detalle"]


def test_ok_si_el_nudging_termina_antes_de_fdda_end(tmp_path):
    # p. ej. una corrida más corta que la ventana de asimilación: no es una violación
    assert nc.analizar_log(_log(tmp_path, [(0.0, 90), (300.0, 100)]), 720)["estado"] == nc.OK


def test_sin_diagnostico_si_se_esperaba_nudging_y_el_log_no_trae_lineas(tmp_path):
    log = tmp_path / "wrf_nudged.log"
    log.write_text("Timing for main (dt=120.00): time 2026-10-22_06:02:00\nwrf: SUCCESS COMPLETE WRF\n")
    assert nc.analizar_log(log, 720, esperaba_nudging=True)["estado"] == nc.SIN_DIAGNOSTICO
    assert nc.analizar_log(log, 720, esperaba_nudging=False)["estado"] == nc.SIN_NUDGING


def test_sin_log(tmp_path):
    r = nc.analizar_log(tmp_path / "no_existe.log", 720)
    assert r["estado"] == nc.SIN_LOG and r["ultimo_xtime_min"] is None


def test_base_namelist_coincide_con_la_asimilacion_por_defecto():
    """El namelist base no debe dejar el nudging activo todo el run si alguien lo usa sin el pipeline."""
    spec = importlib.util.spec_from_file_location("pipeline_diario", ROOT / "scripts" / "pipeline_diario.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pipeline_diario"] = mod
    spec.loader.exec_module(mod)
    texto = (ROOT / "namelist.input").read_text(encoding="utf-8")
    fdda_end = float(re.search(r"^\s*fdda_end\s*=\s*([\d.]+)", texto, re.MULTILINE).group(1))
    assert fdda_end == mod.ASIM_HOURS_DEFAULT * 60


@pytest.mark.skipif(not (ROOT / "outputs/2026-10-22_06z/logs/wrf_nudged.log").exists(),
                    reason="log real del ciclo 22/10 no disponible")
def test_log_real_del_ciclo_22_10_apago_el_nudging_en_t0():
    r = nc.analizar_log(ROOT / "outputs/2026-10-22_06z/logs/wrf_nudged.log", 720)
    assert r["estado"] == nc.OK and r["ultimo_xtime_min"] == 720.0
