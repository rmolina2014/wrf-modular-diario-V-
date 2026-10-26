"""Interfaz de usuario del pronóstico diario WRF (San Juan).

Permite iniciar una corrida y ver los resultados: una tabla con una fila por hora local y una
columna por variable meteorológica, el estado/log de la corrida y los mapas del ciclo.

Uso (desde la raíz del repo, con el entorno que tiene streamlit instalado):

    streamlit run app/streamlit_app.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ui import control, valores  # noqa: E402

OUTPUTS = ROOT / "outputs"
HUSO_LOCAL_H = -3

st.set_page_config(page_title="Pronóstico WRF - San Juan", layout="wide")


# --------------------------------------------------------------------------
# Utilidades de presentación
# --------------------------------------------------------------------------
def _mostrar_tabla(obj) -> None:
    """st.dataframe ocupando todo el ancho (la API del ancho cambió entre versiones)."""
    try:
        st.dataframe(obj, hide_index=True, width="stretch")
    except Exception:  # versiones viejas de Streamlit
        st.dataframe(obj, hide_index=True, use_container_width=True)


def _estilos(tabla: pd.DataFrame, df: pd.DataFrame, mascara: pd.DataFrame) -> pd.DataFrame:
    """Gris suave en las filas de asimilación y rojo en los valores fuera de rango físico."""
    est = pd.DataFrame("", index=tabla.index, columns=tabla.columns)
    est.loc[(df["fase"] == "asimilacion").values, :] = "background-color: rgba(128,128,128,0.15)"
    for col in mascara.columns:
        rotulo = valores.COLUMNAS_ESTACION.get(col)
        if rotulo in est.columns:
            est.loc[mascara[col].values, rotulo] = "background-color:#f8b4b4;color:#7a0000;font-weight:600"
    return est


def _formato(tabla: pd.DataFrame) -> dict:
    return {c: ("{:.2f}" if "Lluvia" in c else "{:.1f}") for c in tabla.columns
            if c not in ("Hora local", "Fase")}


def _etiqueta_caso(caso: Path) -> str:
    estado = valores.cargar_estado(caso).get("estado", "")
    return f"{caso.name[:10]} {caso.name[11:13]}Z" + (f"  ({estado})" if estado else "")


# --------------------------------------------------------------------------
# Barra lateral: iniciar corrida
# --------------------------------------------------------------------------
def panel_lanzamiento() -> None:
    st.sidebar.header("Iniciar corrida")
    ahora = datetime.now(timezone.utc)
    est = control.estado_corrida(ROOT)
    if est["en_curso"]:
        st.sidebar.warning(f"Hay una corrida en curso (PID {est['pid']}, desde {est['desde']}).")

    with st.sidebar.form("form_corrida"):
        fecha = st.date_input("Fecha del ciclo (UTC)", value=ahora.date())
        ciclo = st.selectbox("Ciclo GFS (UTC)", control.CICLOS, index=1)
        asim = st.number_input("Horas de asimilación", min_value=control.ASIM_MIN,
                               max_value=control.ASIM_MAX, value=12, step=1)
        holdout = st.checkbox("Reservar estaciones de evaluación (hold-out)",
                              help="No las asimila, para verificar de forma independiente.")
        dry = st.checkbox("Solo comprobar disponibilidad (dry-run)",
                          help="Revisa observaciones y GFS sin correr WRF.")
        sin_obs = st.checkbox("Continuar sin observaciones (GFS puro)")
        enviar = st.form_submit_button("Iniciar")

    st.sidebar.caption("Solo se asimilan las estaciones que aportan datos suficientes en la ventana.")

    if not enviar:
        return
    if est["en_curso"] and not dry:
        st.sidebar.error("Ya hay una corrida en curso: esperá a que termine.")
        return
    try:
        cmd = control.construir_comando(ROOT, fecha.isoformat(), ciclo, int(asim),
                                        holdout=holdout, dry_run=dry, continue_sin_obs=sin_obs)
    except ValueError as e:
        st.sidebar.error(str(e))
        return
    t0 = datetime.fromisoformat(fecha.isoformat()) + timedelta(hours=int(ciclo) + int(asim))
    if not dry and ahora.replace(tzinfo=None) < t0:
        st.sidebar.warning(f"Todavía no es t0 ({t0:%d/%m %H}Z UTC): se asimilarán menos observaciones.")
    try:
        info = control.lanzar(ROOT, cmd)
    except OSError as e:
        st.sidebar.error(f"No se pudo lanzar la corrida: {e}")
        return
    st.sidebar.success(f"Corrida lanzada (PID {info['pid']}). Seguila en la pestaña «Estado y log».")


# --------------------------------------------------------------------------
# Pestañas
# --------------------------------------------------------------------------
def vista_pronostico(casos: list) -> None:
    caso = st.selectbox("Ciclo", casos, format_func=_etiqueta_caso, key="caso_pron")
    estado = valores.cargar_estado(caso)
    dia_obj = estado.get("dia_objetivo_local")

    tipo_rot = st.radio("Datos", ["Estaciones", "Dominio (resumen)"], horizontal=True)
    tipo = "estaciones" if tipo_rot == "Estaciones" else "dominio"
    df = valores.cargar_valores(caso, tipo)
    if df is None:
        st.info("Este ciclo todavía no tiene valores de pronóstico (la corrida no terminó o falló).")
        return

    col1, col2 = st.columns([2, 2])
    estacion = None
    if tipo == "estaciones":
        estacion = col1.selectbox("Estación", valores.estaciones_disponibles(df))
    solo_obj = col2.checkbox(f"Solo el día objetivo ({dia_obj})" if dia_obj else "Solo el día objetivo",
                             value=bool(dia_obj), disabled=not dia_obj)

    df_f = valores.filtrar(df, estacion, dia_obj if solo_obj else None)
    if df_f.empty:
        st.info("Sin filas para esa selección.")
        return

    tabla = valores.tabla_presentacion(df_f, tipo)
    if tipo == "estaciones":
        mascara = valores.fuera_de_rango(df_f)
        n_malos = int(mascara.values.sum())
        if n_malos:
            st.warning(f"{n_malos} valor(es) fuera del rango físico plausible, marcados en rojo "
                       "(probable artefacto numérico del modelo; no los uses sin revisarlos).")
        sty = tabla.style.apply(lambda _: _estilos(tabla, df_f, mascara), axis=None).format(_formato(tabla))
        _mostrar_tabla(sty)
        st.caption("Filas en gris: fase de asimilación (antes de t0). Sin sombrear: pronóstico libre.")
        asim, excl = estado.get("estaciones_asimiladas"), estado.get("estaciones_excluidas")
        if asim is not None:
            txt = f"**Estaciones asimiladas:** {', '.join(asim) or 'ninguna'}."
            if excl:
                txt += " **No asimiladas (sin datos suficientes):** " + ", ".join(f"{e} ({n})" for e, n in excl.items()) + "."
            st.markdown(txt)
    else:
        _mostrar_tabla(tabla.style.format(_formato(tabla)))
        st.caption("Estadísticas sobre todo el dominio; los extremos pueden venir de celdas de montaña.")

    nombre = f"pronostico_{caso.name}_{estacion or 'dominio'}.csv"
    st.download_button("Descargar CSV", tabla.to_csv(index=False).encode("utf-8-sig"), nombre, "text/csv")


def vista_estado() -> None:
    est = control.estado_corrida(ROOT)
    if est["en_curso"]:
        st.info(f"Corrida en curso (PID {est['pid']}, desde {est['desde']}). Se actualiza cada 10 s.")
    else:
        st.success("No hay una corrida en curso.")

    casos = valores.listar_casos(OUTPUTS)
    if casos:
        caso = st.selectbox("Ciclo", casos, format_func=_etiqueta_caso, key="caso_estado")
        e = valores.cargar_estado(caso)
        if e:
            c = st.columns(4)
            c[0].metric("Estado", e.get("estado", "-"))
            c[1].metric("t0 (UTC)", e.get("t0", "-"))
            c[2].metric("Registros asimilados", e.get("obs_ventana_asim", "-"))
            c[3].metric("Estaciones asimiladas", e.get("obs_estaciones_asim", "-"))
            if e.get("detalle"):
                st.caption(e["detalle"])
            if e.get("estaciones_excluidas"):
                st.warning("No asimiladas por falta de datos (registros en la ventana): "
                           + ", ".join(f"{k} ({v})" for k, v in e["estaciones_excluidas"].items()))

    ruta, lineas = control.ultimo_log(ROOT)
    st.subheader("Log de la última corrida")
    if ruta is None:
        st.caption("Todavía no hay logs en esta máquina.")
    else:
        st.caption(ruta.name)
        st.code("\n".join(lineas) or "(vacío)", language=None)


if hasattr(st, "fragment"):  # refresco automático sin recargar toda la página
    vista_estado = st.fragment(run_every="10s")(vista_estado)


def vista_mapas(casos: list) -> None:
    caso = st.selectbox("Ciclo", casos, format_func=_etiqueta_caso, key="caso_mapas")
    imagenes = sorted((caso / "productos").glob("producto_*.png"))
    if not imagenes:
        st.info("Este ciclo no tiene mapas.")
        return
    cols = st.columns(3)
    for i, img in enumerate(imagenes):
        try:
            utc = datetime.strptime(img.stem.replace("producto_", ""), "%Y%m%d_%H%M%S")
            leyenda = f"{(utc + timedelta(hours=HUSO_LOCAL_H)):%d/%m %H:%M} local"
        except ValueError:
            leyenda = img.stem
        cols[i % 3].image(str(img), caption=leyenda)


# --------------------------------------------------------------------------
# Página
# --------------------------------------------------------------------------
st.title("Pronóstico diario WRF — San Juan")
panel_lanzamiento()

casos_disp = valores.listar_casos(OUTPUTS)
tab_pron, tab_estado, tab_mapas = st.tabs(["Pronóstico", "Estado y log", "Mapas"])
with tab_pron:
    if casos_disp:
        vista_pronostico(casos_disp)
    else:
        st.info("Todavía no hay ciclos en outputs/. Iniciá una corrida desde la barra lateral.")
with tab_estado:
    vista_estado()
with tab_mapas:
    if casos_disp:
        vista_mapas(casos_disp)
    else:
        st.info("Todavía no hay ciclos en outputs/.")
