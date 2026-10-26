"""Tests de la interfaz: lógica de valores/control y el índice HTML. No ejecutan WRF ni usan la red."""

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ui import control, html_tabla, valores  # noqa: E402

CABECERA = ("fecha_hora_utc,fecha_hora_local,lead_h,fase,estacion,lat,lon,t2_c,rh_pct,psfc_hpa,"
            "viento_kmh,direccion_grados,lluvia_acum_mm\n")


def _caso(tmp_path, viento_malo=False):
    caso = tmp_path / "outputs" / "2026-10-22_06z"
    caso.mkdir(parents=True)
    filas = []
    for est, base in (("A", 15.0), ("B", 10.0)):
        for h in range(6):  # 06Z..11Z = 03..08 local
            utc = f"2026-10-22_{6 + h:02d}:00:00"
            loc = f"2026-10-22_{3 + h:02d}:00:00"
            fase = "asimilacion" if h < 3 else "pronostico_libre"
            v = 237.7 if (viento_malo and est == "B" and h == 1) else 12.0
            lluvia = 0.0 if h < 2 else 1.0 if h < 4 else 1.5
            filas.append(f"{utc},{loc},{h - 3},{fase},{est},-31.5,-68.5,{base + h},60.0,900.0,{v},150,{lluvia}")
    (caso / "valores_pronostico_estaciones.csv").write_text(CABECERA + "\n".join(filas) + "\n")
    (caso / "estado.json").write_text(json.dumps({
        "estado": "EJECUTADO", "dia_objetivo_local": "2026-10-22", "t0": "2026-10-22 09:00",
        "estaciones_asimiladas": ["A", "B"], "estaciones_excluidas": {"C": 0}}))
    return caso


def test_listar_casos_ordena_del_mas_reciente_y_filtra(tmp_path):
    out = tmp_path / "outputs"
    for n in ("2026-10-21_12z", "2026-10-22_06z", "otra_cosa"):
        (out / n).mkdir(parents=True)
    (out / "archivo.txt").write_text("x")
    assert [c.name for c in valores.listar_casos(out)] == ["2026-10-22_06z", "2026-10-21_12z"]
    assert valores.listar_casos(tmp_path / "no_existe") == []


def test_lluvia_horaria_es_la_diferencia_de_la_acumulada(tmp_path):
    df = valores.cargar_valores(_caso(tmp_path))
    a = valores.filtrar(df, "A")
    assert a["lluvia_hora_mm"].tolist() == [0.0, 0.0, 1.0, 0.0, 0.5, 0.0]


def test_filtrar_por_estacion_y_dia(tmp_path):
    df = valores.cargar_valores(_caso(tmp_path))
    assert len(valores.filtrar(df, "A")) == 6
    assert len(valores.filtrar(df, "A", "2026-10-22")) == 6
    assert valores.filtrar(df, "A", "2026-10-23").empty
    assert valores.estaciones_disponibles(df) == ["A", "B"]


def test_csv_viejo_sin_hora_local_ni_fase_ni_lluvia(tmp_path):
    caso = tmp_path / "outputs" / "2026-10-21_12z"
    caso.mkdir(parents=True)
    (caso / "valores_pronostico_estaciones.csv").write_text(
        "fecha_hora_utc,estacion,lat,lon,t2_c,rh_pct,psfc_hpa,viento_kmh,direccion_grados\n"
        "2026-10-21_12:00:00,A,-31.5,-68.5,12.4,24.3,815.6,4.6,165.0\n"
        "2026-10-21_13:00:00,A,-31.5,-68.5,15.6,30.8,815.7,10.9,208.0\n")
    df = valores.cargar_valores(caso)
    assert df["hora_local"].dt.strftime("%H:%M").tolist() == ["09:00", "10:00"]
    tabla = valores.tabla_presentacion(valores.filtrar(df, "A"))
    assert "Lluvia acum. (mm)" not in tabla.columns and "T2 (°C)" in tabla.columns
    assert tabla["Fase"].tolist() == ["-", "-"]
    pagina = html_tabla.construir_indice_html(caso, [])  # no debe fallar sin estado.json
    assert "<table" in pagina


def test_cargar_valores_inexistente_devuelve_none(tmp_path):
    assert valores.cargar_valores(tmp_path, "dominio") is None


def test_tabla_tiene_hora_primero_y_luego_las_variables(tmp_path):
    df = valores.filtrar(valores.cargar_valores(_caso(tmp_path)), "A")
    tabla = valores.tabla_presentacion(df)
    assert list(tabla.columns)[:2] == ["Hora local", "Fase"]
    assert list(tabla.columns)[2:] == list(valores.COLUMNAS_ESTACION.values())
    assert tabla["Hora local"].iloc[0] == "22/10 03:00"
    assert tabla["Fase"].iloc[0] == "Asimilación" and tabla["Fase"].iloc[-1] == "Pronóstico libre"


def test_fuera_de_rango_marca_el_viento_imposible(tmp_path):
    df = valores.filtrar(valores.cargar_valores(_caso(tmp_path, viento_malo=True)), "B")
    mascara = valores.fuera_de_rango(df)
    assert int(mascara.values.sum()) == 1
    assert bool(mascara["viento_kmh"].iloc[1])
    ok = valores.filtrar(valores.cargar_valores(_caso(tmp_path / "otro")), "A")
    assert not valores.fuera_de_rango(ok).values.any()


def test_indice_html_trae_datos_y_marca_valores_malos(tmp_path):
    caso = _caso(tmp_path, viento_malo=True)
    datos = html_tabla.construir_datos(caso)
    assert [f["nombre"] for f in datos["fuentes"]] == ["A", "B"]
    assert datos["objetivo"] == "2026-10-22"
    b = datos["fuentes"][1]["filas"]
    assert b[1]["mal"] == ["viento_kmh"] and "mal" not in b[0]
    pagina = html_tabla.construir_indice_html(caso, [])
    assert "<table" in pagina and "Hora local" in pagina
    assert "estaciones asimiladas: A, B" in pagina and "sin datos suficientes: C" in pagina


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------
def test_construir_comando_valido(tmp_path):
    cmd = control.construir_comando(tmp_path, "2026-10-22", "6", 12, holdout=True, dry_run=True)
    assert cmd[:2] == ["bash", str(tmp_path / "run_diario.sh")]
    assert cmd[2:8] == ["--fecha", "2026-10-22", "--ciclo", "06", "--asim-hours", "12"]
    assert "--holdout" in cmd and "--dry-run" in cmd and "--continue-sin-obs" not in cmd


@pytest.mark.parametrize("kw", [
    {"fecha": "22/10/2026"}, {"fecha": "2026-10-22; rm -rf /"}, {"ciclo": "07"},
    {"asim_hours": 0}, {"asim_hours": 99}, {"asim_hours": "12"}, {"asim_hours": True},
])
def test_construir_comando_rechaza_valores_invalidos(tmp_path, kw):
    args = {"fecha": "2026-10-22", "ciclo": "06", "asim_hours": 12}
    args.update(kw)
    with pytest.raises(ValueError):
        control.construir_comando(tmp_path, **args)


def test_estado_corrida_segun_el_lock(tmp_path):
    assert control.estado_corrida(tmp_path)["en_curso"] is False
    lock = tmp_path / "logs" / "corrida.lock"
    lock.parent.mkdir()
    lock.write_text(f"{os.getpid()} 2026-10-22T10:00:00\n")
    est = control.estado_corrida(tmp_path)
    assert est["en_curso"] is True and est["pid"] == os.getpid()
    lock.write_text("999999999 2026-10-22T10:00:00\n")  # PID inexistente
    assert control.estado_corrida(tmp_path)["en_curso"] is False
    lock.write_text(f"{os.getpid()} x\n")
    viejo = time.time() - 7 * 3600
    os.utime(lock, (viejo, viejo))
    assert control.estado_corrida(tmp_path)["en_curso"] is False


def test_lanzar_corre_desacoplado_y_deja_salida(tmp_path):
    info = control.lanzar(tmp_path, ["bash", "-c", "echo hola"])
    for _ in range(50):
        if info["salida"].read_text().strip():
            break
        time.sleep(0.1)
    assert info["salida"].read_text().strip() == "hola"


def test_ultimo_log_toma_el_mas_reciente(tmp_path):
    assert control.ultimo_log(tmp_path) == (None, [])
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run_a.log").write_text("viejo\n")
    time.sleep(0.02)
    (logs / "run_b.log").write_text("\n".join(f"l{i}" for i in range(100)))
    ruta, lineas = control.ultimo_log(tmp_path, n=3)
    assert ruta.name == "run_b.log" and lineas == ["l97", "l98", "l99"]


# --------------------------------------------------------------------------
# App de Streamlit (se omite si no está instalado)
# --------------------------------------------------------------------------
def test_app_streamlit_arranca_sin_excepciones():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Pronóstico", "Estado y log", "Mapas"]
