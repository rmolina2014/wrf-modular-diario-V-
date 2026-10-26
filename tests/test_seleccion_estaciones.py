"""Tests de la selección de estaciones a asimilar.

Una estación que no aporta datos suficientes en la ventana [start, t0] no debe entrar a
obs_flat, Little_R ni OBS_DOMAIN101. No ejecutan WRF ni acceden a la red.
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("pipeline_diario", ROOT / "scripts" / "pipeline_diario.py")
pd_mod = importlib.util.module_from_spec(_spec)
sys.modules["pipeline_diario"] = pd_mod
_spec.loader.exec_module(pd_mod)

START = datetime(2026, 10, 22, 6)
T0 = datetime(2026, 10, 22, 18)  # 12 h a 5 min -> 144 registros esperables por estación


def test_estacion_con_pocos_registros_se_excluye():
    por_est = {"A": 141, "B": 142, "INTA_POCITO": 6}
    validas, excluidas, minimo = pd_mod.seleccionar_estaciones(por_est, START, T0)
    assert minimo == 72  # 50 % de 144
    assert validas == {"A", "B"}
    assert excluidas == {"INTA_POCITO": 6}


def test_estacion_sin_ningun_dato_se_informa_como_excluida():
    por_est = {"A": 140}
    validas, excluidas, _ = pd_mod.seleccionar_estaciones(
        por_est, START, T0, candidatas={"A", "LOS_PIONEROS"})
    assert validas == {"A"}
    assert excluidas == {"LOS_PIONEROS": 0}


def test_el_umbral_se_ajusta_a_la_ventana_de_asimilacion():
    # Con 6 h de asimilación se esperan 72 registros: el mínimo baja a 36.
    _, _, minimo = pd_mod.seleccionar_estaciones({}, START, datetime(2026, 10, 22, 12))
    assert minimo == 36


def test_sin_estaciones_validas_queda_vacio():
    validas, excluidas, _ = pd_mod.seleccionar_estaciones({"A": 3, "B": 10}, START, T0)
    assert validas == set()
    assert excluidas == {"A": 3, "B": 10}


def test_cobertura_se_recalcula_solo_con_las_estaciones_validas(tmp_path):
    filas = []
    base = int(START.replace(tzinfo=pd_mod.timezone.utc).timestamp())
    for est, n in (("A", 140), ("B", 138), ("POCO", 6)):
        for i in range(n):
            filas.append({"estacion": est, "hora_unix": base + 300 * i, "temp_c": 15.0})
    csv = tmp_path / "obs.csv"
    pd.DataFrame(filas).to_csv(csv, index=False)

    cov = pd_mod.cobertura_obs(csv, START, T0)
    validas, excluidas, _ = pd_mod.seleccionar_estaciones(cov["por_estacion"], START, T0)
    cov2 = pd_mod.cobertura_obs(csv, START, T0, asimilables=validas)

    assert excluidas == {"POCO": 6}
    assert cov2["estaciones_asim"] == 2
    assert cov2["ventana_asim"] == 140 + 138
    # La cobertura global de la ventana no cambia: sigue contando las 3 estaciones.
    assert cov2["estaciones"] == 3
