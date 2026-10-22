"""Tests de métricas y armado de pares para la verificación diferida."""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.verificacion import metricas as M  # noqa: E402
from src.verificacion import pares as P  # noqa: E402


def test_resumen_continuo_conocido():
    r = M.resumen([0, 0, 0, 0], [1, -1, 2, -2])
    assert r["n"] == 4
    assert r["bias"] == 0
    assert r["mae"] == 1.5
    assert r["rmse"] == np.sqrt(2.5)


def test_resumen_ignora_nan():
    r = M.resumen([1, np.nan, 3], [2, 5, np.nan])
    assert r["n"] == 1
    assert r["bias"] == 1


def test_direccion_circular():
    r = M.resumen_direccion([350, 10], [10, 350])
    assert r["mae"] == 20
    assert r["bias"] == 0


def test_categoricas_tabla_contingencia():
    obs = [0, 0, 5, 5, 0]
    mod = [0, 5, 5, 0, 0]
    r = M.categoricas(obs, mod, umbral=1)
    assert (r["aciertos"], r["falsas_alarmas"], r["faltantes"], r["rechazos"]) == (1, 1, 1, 2)
    assert r["pod"] == 0.5
    assert r["far"] == 0.5
    assert r["csi"] == 1 / 3


def test_agregar_horario_viento_vectorial():
    ts = pd.to_datetime(["2026-10-22 12:00", "2026-10-22 12:10"], utc=True).tz_localize(None)
    obs = pd.DataFrame({
        "estacion": ["A", "A"], "ts": ts,
        "temp_c": [10.0, 12.0], "humedad_pct": [50.0, 60.0],
        "presion_absoluta_hpa": [900.0, 902.0],
        "viento_kmh": [10.0, 10.0], "direcc_grados": [0.0, 180.0],
    })
    h = P.agregar_horario(obs)
    assert len(h) == 1
    assert h.iloc[0]["t2_c"] == 11
    assert h.iloc[0]["viento_kmh"] < 1e-6
    assert h.iloc[0]["n_obs"] == 2


def test_cargar_modelo_deriva_lead_y_fase(tmp_path):
    csv = tmp_path / "valores_pronostico_estaciones.csv"
    pd.DataFrame({
        "fecha_hora_utc": ["2026-10-21_12:00:00", "2026-10-21_18:00:00"],
        "estacion": ["A", "A"], "t2_c": [10.0, 11.0],
    }).to_csv(csv, index=False)
    df = P.cargar_modelo(tmp_path, t0=datetime(2026, 10, 21, 15))
    assert df["lead_h"].tolist() == [-3.0, 3.0]
    assert df["fase"].tolist() == ["asimilacion", "pronostico_libre"]


def test_emparejar_por_hora_exacta():
    modelo = pd.DataFrame({
        "estacion": ["A", "A"], "valid_utc": pd.to_datetime(["2026-10-22 12:00", "2026-10-22 13:00"]),
        "t2_c": [10.0, 11.0], "lead_h": [12.0, 13.0], "fase": ["pronostico_libre"] * 2,
    })
    obs = pd.DataFrame({
        "estacion": ["A", "B"], "valid_utc": pd.to_datetime(["2026-10-22 12:00", "2026-10-22 12:00"]),
        "t2_c": [9.0, 9.0], "rh_pct": [50.0, 50.0], "psfc_hpa": [900.0, 900.0],
        "viento_kmh": [5.0, 5.0], "direccion_grados": [90.0, 90.0],
    })
    df = P.emparejar(modelo, obs)
    assert len(df) == 1
    assert df.iloc[0]["obs_t2_c"] == 9
    assert df.iloc[0]["lead_h"] == 12


import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("verificar_diario", ROOT / "scripts" / "verificar_diario.py")
vd = importlib.util.module_from_spec(_spec)
sys.modules["verificar_diario"] = vd
_spec.loader.exec_module(vd)


def _modelo_horario():
    horas = pd.date_range("2026-10-22 00:00", periods=3, freq="h")
    return pd.DataFrame({
        "estacion": ["A"] * 3, "valid_utc": horas, "t2_c": [10.0, 11.0, 12.0],
        "lead_h": [1.0, 2.0, 3.0],
        "obs_t2_c": [9.0, 10.0, 11.0],
    })


def test_extremos_ventanas_smn():
    horas = pd.date_range("2026-10-22 00:00", periods=24, freq="h")
    modelo = pd.DataFrame({"estacion": ["A"] * 24, "valid_utc": horas,
                           "t2_c": np.arange(24, dtype=float), "lead_h": np.arange(1, 25)})
    ts = pd.date_range("2026-10-22 00:00", periods=144, freq="10min")
    obs = pd.DataFrame({"estacion": ["A"] * 144, "ts": ts,
                        "temp_c": np.repeat(np.arange(24, dtype=float), 6)})
    ext = vd._extremos(modelo, obs, datetime(2026, 10, 22, 0), datetime(2026, 10, 22, 23))
    assert len(ext) == 2
    assert set(ext["ventana"]) == {"00-12Z", "12-00Z"}
    assert ext[ext["ventana"] == "00-12Z"].iloc[0]["tmax_modelo"] == 11
    ext2 = vd._extremos(modelo, obs.iloc[:36], datetime(2026, 10, 22, 0), datetime(2026, 10, 22, 23))
    assert ext2.empty


def test_lluvia_diaria_24h():
    horas = pd.to_datetime(["2026-10-22 03:00", "2026-10-22 15:00", "2026-10-23 03:00"])
    modelo = pd.DataFrame({"estacion": ["A"] * 3, "valid_utc": horas,
                           "lluvia_acum_mm": [0.0, 1.0, 5.0]})
    ts = pd.date_range("2026-10-22 03:00", "2026-10-23 02:50", freq="10min")
    obs = pd.DataFrame({"estacion": ["A"] * len(ts), "ts": ts,
                        "lluvia_diaria_mm": np.linspace(0, 5, len(ts))})
    ll = vd._lluvia_diaria(modelo, obs, datetime(2026, 10, 22, 3), datetime(2026, 10, 23, 3))
    assert len(ll) == 1
    assert ll.iloc[0]["lluvia_modelo_mm"] == 5
    assert abs(ll.iloc[0]["error_mm"]) < 0.01


def test_persistencia_parte_de_t0():
    modelo = _modelo_horario()
    obs_h = pd.DataFrame({
        "estacion": ["A"], "valid_utc": pd.to_datetime(["2026-10-22 00:00"]),
        "t2_c": [7.0], "rh_pct": [40.0], "psfc_hpa": [900.0],
        "viento_kmh": [5.0], "direccion_grados": [90.0],
    })
    p = vd._persistencia(modelo, obs_h, datetime(2026, 10, 22, 0, 30))
    assert p["t2_c"].tolist() == [7.0, 7.0, 7.0]
