"""Tests del protocolo temporal: asimilación hasta t0 + pronóstico libre.

No ejecutan WRF ni acceden a la red: solo la lógica de ventanas, el filtrado de
observaciones y el chequeo de preflight 2.2.7.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("pipeline_diario", ROOT / "scripts" / "pipeline_diario.py")
pd_mod = importlib.util.module_from_spec(_spec)
sys.modules["pipeline_diario"] = pd_mod
_spec.loader.exec_module(pd_mod)

from src.preflight.preflight import Estado, correr_preflight_observaciones  # noqa: E402


# --------------------------------------------------------------------------
# calcular_ventana
# --------------------------------------------------------------------------
def test_ventana_por_defecto_cubre_el_dia_local_siguiente():
    v = pd_mod.calcular_ventana("2026-10-22", "06", 12)
    assert v["start"] == datetime(2026, 10, 22, 6)
    assert v["t0"] == datetime(2026, 10, 22, 18)
    assert v["horizon_h"] == 33 and v["run_hours"] == 45
    # día objetivo = 23/10 local (UTC-3): 03Z del 23 a 03Z del 24
    assert str(v["dia_objetivo"]) == "2026-10-23"
    assert v["valid_times"][0] == "2026-10-23_03:00:00"
    assert v["valid_times"][-1] == "2026-10-24_03:00:00"
    assert len(v["valid_times"]) == 9
    assert v["cubre_dia_objetivo"]


def test_tiempos_validos_solo_en_pronostico_libre():
    v = pd_mod.calcular_ventana("2026-10-22", "06", 12)
    t0 = v["t0"].strftime("%Y-%m-%d_%H:%M:%S")
    assert all(t >= t0 for t in v["valid_times"])


def test_run_hours_y_horizon_h_equivalentes():
    a = pd_mod.calcular_ventana("2026-10-22", "06", 12, horizon_h=24)
    b = pd_mod.calcular_ventana("2026-10-22", "06", 12, run_hours=36)
    assert a["run_hours"] == b["run_hours"] == 36


def test_horizonte_corto_no_cubre_el_dia():
    v = pd_mod.calcular_ventana("2026-10-22", "06", 12, horizon_h=12)
    assert not v["cubre_dia_objetivo"]


@pytest.mark.parametrize("kwargs", [
    dict(asim_hours=0),                          # sin asimilación
    dict(asim_hours=12, horizon_h=0),            # sin pronóstico libre
    dict(asim_hours=12, horizon_h=10),           # 22 h: no múltiplo de 3
    dict(asim_hours=12, horizon_h=24, run_hours=40),  # incoherentes entre sí
])
def test_ventanas_invalidas(kwargs):
    with pytest.raises(ValueError):
        pd_mod.calcular_ventana("2026-10-22", "06", **kwargs)


# --------------------------------------------------------------------------
# Observaciones de la ventana
# --------------------------------------------------------------------------
def test_csv_cubre_con_tolerancia(tmp_path):
    csv = tmp_path / "a.csv"
    pd.DataFrame({"estacion": ["X", "X"], "hora_unix": [1000, 2000]}).to_csv(csv, index=False)
    assert pd_mod._csv_cubre(csv, 2000)
    assert pd_mod._csv_cubre(csv, 2900)       # dentro de la tolerancia de 15 min
    assert not pd_mod._csv_cubre(csv, 3000)   # CSV viejo: hay que volver a descargar


def test_obtener_obs_ventana_fusiona_dias_sin_duplicados(tmp_path, monkeypatch):
    monkeypatch.setattr(pd_mod, "DATA_RAW", tmp_path)
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    pd.DataFrame({"estacion": ["X"] * 3, "hora_unix": [1, 2, 3]}).to_csv(a, index=False)
    pd.DataFrame({"estacion": ["X"] * 2, "hora_unix": [3, 4]}).to_csv(b, index=False)
    llamadas = []

    def descargar_falso(fecha, fuerza=False, hasta_ts=None):
        llamadas.append((fecha, hasta_ts))
        return {"2026-10-21": a, "2026-10-22": b}[fecha]

    monkeypatch.setattr(pd_mod, "descargar_obs", descargar_falso)
    t0 = datetime(2026, 10, 22, 6)
    csv = pd_mod.obtener_obs_ventana(datetime(2026, 10, 22, 0), t0)   # ciclo 00Z: toca dos días
    assert [f for f, _ in llamadas] == ["2026-10-21", "2026-10-22"]
    assert llamadas[0][1] is None                      # solo el último día debe llegar hasta t0
    assert llamadas[1][1] == int(t0.replace(tzinfo=timezone.utc).timestamp())
    assert list(pd.read_csv(csv)["hora_unix"]) == [1, 2, 3, 4]


# --------------------------------------------------------------------------
# Preflight 2.2.7 (separación temporal)
# --------------------------------------------------------------------------
def _obsdomain(path: Path, timestamps):
    """OBS_DOMAIN101 mínimo (FORMAT 105, 5 líneas por observación)."""
    datos = (" -888888.000 -888888.000 -888888.000 -888888.000"
             "     615.000       0.000     290.000       0.000"
             "       1.000       0.000       1.000       0.000"
             "      40.000       0.000   95000.000       0.000"
             " -888888.000 -888888.000")
    bloques = []
    for ts in timestamps:
        bloques += [f" {ts}", "   -31.6500  -68.5833 ",
                    f"  {'EST':<40s}   {'SURFACE':<40s}   ",
                    f"  {'FM-12 SYNOP':<16s}  {'EST':<16s}       615  F     F         1",
                    datos]
    path.write_text("\n".join(bloques) + "\n", encoding="utf-8")


def _namelist(path: Path, fdda_end: int):
    path.write_text(f" max_obs = 10000,\n  fdda_end = {fdda_end},\n run_hours = 45,\n", encoding="utf-8")


def _preflight(tmp_path, timestamps, fdda_end):
    obs_json = tmp_path / "obs.json"
    obs_json.write_text("[{}]", encoding="utf-8")
    obsdomain, namelist = tmp_path / "OBS_DOMAIN101", tmp_path / "namelist.input"
    _obsdomain(obsdomain, timestamps)
    _namelist(namelist, fdda_end)
    informe = correr_preflight_observaciones(
        obs_json, obsdomain, namelist, start_dt=datetime(2026, 10, 22, 6), caso="test",
        config_path=ROOT / "config" / "preflight_config.json", t0=datetime(2026, 10, 22, 18))
    return {c.id: c for c in informe.chequeos}


def test_preflight_ok_con_obs_dentro_de_la_ventana(tmp_path):
    ts = ["20261022060000", "20261022120000", "20261022180000"]  # incluye t0 exacto
    c = _preflight(tmp_path, ts, fdda_end=720)
    assert c["2.2.7"].estado == Estado.OK


def test_preflight_bloquea_obs_posterior_a_t0(tmp_path):
    ts = ["20261022120000", "20261022180500"]  # 5 min después de t0
    c = _preflight(tmp_path, ts, fdda_end=720)
    assert c["2.2.7"].estado == Estado.BLOQUEANTE
    assert "posteriores a t0" in c["2.2.7"].detalle


def test_preflight_bloquea_obs_anterior_al_inicio(tmp_path):
    ts = ["20261022055500", "20261022120000"]
    c = _preflight(tmp_path, ts, fdda_end=720)
    assert c["2.2.7"].estado == Estado.BLOQUEANTE
    assert "anteriores al inicio" in c["2.2.7"].detalle


def test_preflight_bloquea_nudging_continuo(tmp_path):
    ts = ["20261022120000"]
    c = _preflight(tmp_path, ts, fdda_end=2700)  # 45 h: nudging hasta el final
    assert c["2.2.7"].estado == Estado.BLOQUEANTE
    assert "fdda_end" in c["2.2.7"].detalle


def test_cobertura_se_mide_sobre_la_ventana_de_asimilacion(tmp_path):
    # Una obs cada 30 min entre 06Z y 18Z. Medida sobre toda la corrida (45 h) dejaría
    # vacíos los tercios 2 y 3; medida sobre [start, t0] los tres tercios tienen datos.
    inicio = datetime(2026, 10, 22, 6)
    ts = [(inicio + timedelta(minutes=30 * i)).strftime("%Y%m%d%H%M%S") for i in range(25)]
    c = _preflight(tmp_path, ts, fdda_end=720)
    assert c["2.2.6"].estado == Estado.OK, c["2.2.6"].detalle


# --------------------------------------------------------------------------
# Sin hold-out: todas las estaciones con datos se asimilan
# --------------------------------------------------------------------------
def test_obsdomain_incluye_estaciones_rol_evaluacion_por_defecto(tmp_path):
    from src.asimilacion.obsnud_writer import ObsNudWriter

    df = pd.DataFrame([
        {"estacion": "A", "fecha": "2026-10-22", "hora": "07:00:00", "temp": 20.0,
         "humedad": 40.0, "presion_absoluta": 950.0, "viento": 10.0, "direcc": 90.0},
        {"estacion": "B", "fecha": "2026-10-22", "hora": "07:00:00", "temp": 21.0,
         "humedad": 41.0, "presion_absoluta": 951.0, "viento": 11.0, "direcc": 91.0},
    ])
    cfg = {"A": {"lat": -31.5, "lon": -68.5, "elev": 700, "rol": "asimilacion"},
           "B": {"lat": -31.6, "lon": -68.6, "elev": 710, "rol": "evaluacion"}}
    w = ObsNudWriter()
    out = w.generar_desde_dataframe(df, cfg, str(tmp_path / "OBS_A"))
    txt = out.read_text()
    assert "A" in txt and txt.count("SURFACE") == 2
    out2 = w.generar_desde_dataframe(df, cfg, str(tmp_path / "OBS_B"), usar_roles=True)
    assert out2.read_text().count("SURFACE") == 1
