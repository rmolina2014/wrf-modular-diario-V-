"""Tests de los 4 ciclos diarios: día objetivo anclado y hold-out opt-in."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("pipeline_diario", ROOT / "scripts" / "pipeline_diario.py")
pd_mod = importlib.util.module_from_spec(_spec)
sys.modules["pipeline_diario"] = pd_mod
_spec.loader.exec_module(pd_mod)


@pytest.mark.parametrize("ciclo, total, horizonte", [
    ("00", 51, 39),
    ("06", 45, 33),
    ("12", 39, 27),
    ("18", 33, 21),
])
def test_cuatro_ciclos_cubren_el_mismo_dia_objetivo(ciclo, total, horizonte):
    v = pd_mod.calcular_ventana("2026-10-22", ciclo, 12, dia_objetivo="2026-10-23")
    assert v["run_hours"] == total
    assert v["horizon_h"] == horizonte
    assert str(v["dia_objetivo"]) == "2026-10-23"
    assert v["objetivo_ini"] == datetime(2026, 10, 23, 3)
    assert v["objetivo_fin"] == datetime(2026, 10, 24, 3)
    assert v["cubre_dia_objetivo"]
    assert v["valid_times"][0] >= v["t0"].strftime("%Y-%m-%d_%H:%M:%S")
    assert v["valid_times"][-1] == "2026-10-24_03:00:00"


def test_dia_objetivo_anterior_a_t0_es_invalido():
    with pytest.raises(ValueError):
        pd_mod.calcular_ventana("2026-10-22", "18", 12, dia_objetivo="2026-10-22")


def test_cobertura_separa_asimilables_del_holdout(tmp_path):
    csv = tmp_path / "obs.csv"
    ts = int(datetime(2026, 10, 22, 12).timestamp())
    pd.DataFrame({
        "estacion": ["A", "A", "B", "B"],
        "hora_unix": [ts, ts + 600, ts, ts + 600],
        "temp_c": [20.0, 21.0, 22.0, 23.0],
    }).to_csv(csv, index=False)
    cov = pd_mod.cobertura_obs(csv, datetime(2026, 10, 22, 6),
                               datetime(2026, 10, 22, 18), asimilables={"A"})
    assert cov["ventana"] == 4 and cov["estaciones"] == 2
    assert cov["ventana_asim"] == 2 and cov["estaciones_asim"] == 1


def test_holdout_deja_solo_asimilables(tmp_path):
    from src.asimilacion.obsnud_writer import ObsNudWriter

    df = pd.DataFrame([
        {"estacion": "A", "fecha": "2026-10-22", "hora": "07:00:00", "temp": 20.0,
         "humedad": 40.0, "presion_absoluta": 950.0, "viento": 10.0, "direcc": 90.0},
        {"estacion": "B", "fecha": "2026-10-22", "hora": "07:00:00", "temp": 21.0,
         "humedad": 41.0, "presion_absoluta": 951.0, "viento": 11.0, "direcc": 91.0},
    ])
    cfg = {"A": {"lat": -31.5, "lon": -68.5, "elev": 700, "rol": "asimilacion"},
           "B": {"lat": -31.6, "lon": -68.6, "elev": 710, "rol": "evaluacion"}}
    out = ObsNudWriter().generar_desde_dataframe(df, cfg, str(tmp_path / "OBS"), usar_roles=True)
    assert out.read_text().count("SURFACE") == 1


def test_dry_run_no_pisa_estado_ejecutado(tmp_path):
    (tmp_path / "estado.json").write_text('{"estado": "EJECUTADO"}', encoding="utf-8")
    pd_mod.escribir_informe(tmp_path, {"fecha": "2026-10-22", "ciclo": "06", "estado": "DRY_RUN"},
                            sufijo="dry_run")
    assert (tmp_path / "estado_dry_run.json").exists()
    assert (tmp_path / "informe_dry_run.md").exists()
    assert '"EJECUTADO"' in (tmp_path / "estado.json").read_text(encoding="utf-8")
