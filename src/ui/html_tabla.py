"""Índice HTML estático del ciclo: tabla por hora y variable (+ mapas), sin servidor.

Es un único archivo autocontenido (CSS y JS en línea). Los datos van embebidos como JSON y
una lista desplegable elige la estación, así que funciona abriendo el archivo en el navegador.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List

from src.ui import valores

_PLANTILLA = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pronóstico WRF - __TITULO__</title>
<style>
 body{font-family:system-ui,sans-serif;margin:1.5rem;color:#1b1f24}
 h1{font-size:1.4rem;margin:0 0 .2rem} h2{font-size:1.1rem;margin:1.6rem 0 .6rem}
 .sub{color:#57606a;margin:0 0 1rem}
 .ctl{display:flex;gap:1.2rem;flex-wrap:wrap;align-items:center;margin:.6rem 0}
 table{border-collapse:collapse;font-variant-numeric:tabular-nums}
 th,td{border:1px solid #d0d7de;padding:.3rem .7rem;text-align:right}
 th{background:#f6f8fa;position:sticky;top:0} td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){text-align:left}
 tr.asim td{background:#f0f0f0;color:#555} td.mal{background:#f8b4b4;color:#7a0000;font-weight:600}
 .aviso{background:#fff8c5;border:1px solid #d4a72c;padding:.4rem .7rem;margin:.6rem 0;display:none}
 .mapas{display:flex;flex-wrap:wrap;gap:.8rem} .mapas figure{margin:0}
 .mapas img{width:320px;border:1px solid #d0d7de} .mapas figcaption{font-size:.85rem;color:#57606a}
 .nota{font-size:.85rem;color:#57606a}
</style></head><body>
<h1>Pronóstico WRF — San Juan</h1>
<p class="sub">__SUBTITULO__</p>
<div class="ctl">
 <label>Datos <select id="fuente"></select></label>
 <label id="lblObj"><input type="checkbox" id="soloObj" checked> Solo el día objetivo (__OBJETIVO__)</label>
</div>
<div class="aviso" id="aviso"></div>
<table id="tabla"><thead></thead><tbody></tbody></table>
<p class="nota">Filas en gris: fase de asimilación (antes de t0). Sin sombrear: pronóstico libre.
Celdas rojas: valor fuera del rango físico plausible (probable artefacto del modelo).</p>
<h2>Mapas</h2>
<div class="mapas">__MAPAS__</div>
<script>
const D = __DATOS__;
const sel = document.getElementById('fuente'), chk = document.getElementById('soloObj');
const thead = document.querySelector('#tabla thead'), tbody = document.querySelector('#tabla tbody');
const aviso = document.getElementById('aviso');
if (!D.objetivo) { document.getElementById('lblObj').style.display = 'none'; }
D.fuentes.forEach((f, i) => { const o = document.createElement('option'); o.value = i; o.textContent = f.nombre; sel.appendChild(o); });
function fmt(v, c) { if (v === null || v === undefined) return '-'; return (typeof v === 'number') ? v.toFixed(c.dec) : v; }
function pintar() {
  const f = D.fuentes[+sel.value];
  thead.innerHTML = '<tr>' + f.columnas.map(c => '<th>' + c.rotulo + '</th>').join('') + '</tr>';
  let filas = f.filas;
  if (chk.checked && D.objetivo) filas = filas.filter(r => r.dia === D.objetivo);
  let malos = 0;
  tbody.innerHTML = filas.map(r => {
    const celdas = f.columnas.map(c => {
      const mal = r.mal && r.mal.includes(c.clave); if (mal) malos++;
      return '<td' + (mal ? ' class="mal"' : '') + '>' + fmt(r[c.clave], c) + '</td>';
    }).join('');
    return '<tr' + (r.fase_clave === 'asimilacion' ? ' class="asim"' : '') + '>' + celdas + '</tr>';
  }).join('');
  aviso.style.display = malos ? 'block' : 'none';
  aviso.textContent = malos + ' valor(es) fuera del rango físico plausible (en rojo): revisalos antes de usarlos.';
}
sel.addEventListener('change', pintar); chk.addEventListener('change', pintar); pintar();
</script></body></html>
"""


def _fila(r: Dict[str, Any], claves: List[str], mal: List[str]) -> Dict[str, Any]:
    fila = {"hora": r["hora"], "fase": r["fase"], "fase_clave": r["fase_clave"], "dia": r["dia"]}
    for c in claves:
        v = r.get(c)
        fila[c] = None if v != v else round(float(v), 2)  # NaN -> null
    if mal:
        fila["mal"] = mal
    return fila


def _fuente(nombre: str, df, tipo: str) -> Dict[str, Any]:
    cols = valores.COLUMNAS_ESTACION if tipo == "estaciones" else valores.COLUMNAS_DOMINIO
    claves = [c for c in cols if c in df.columns]
    mascara = valores.fuera_de_rango(df) if tipo == "estaciones" else None
    filas = []
    for i, r in df.reset_index(drop=True).iterrows():
        base = {
            "hora": r["hora_local"].strftime("%d/%m %H:%M"),
            "fase": valores.FASES.get(r["fase"], r["fase"]),
            "fase_clave": r["fase"],
            "dia": r["hora_local"].strftime("%Y-%m-%d"),
            **{c: r[c] for c in claves},
        }
        mal = [c for c in mascara.columns if mascara.at[i, c]] if mascara is not None else []
        filas.append(_fila(base, claves, mal))
    columnas = [{"clave": "hora", "rotulo": "Hora local", "dec": 0},
                {"clave": "fase", "rotulo": "Fase", "dec": 0}]
    columnas += [{"clave": c, "rotulo": cols[c], "dec": 2 if "lluvia" in c else 1} for c in claves]
    return {"nombre": nombre, "columnas": columnas, "filas": filas}


def construir_datos(caso_dir: Path) -> Dict[str, Any]:
    """Estructura JSON con una fuente por estación (y el resumen del dominio, si existe)."""
    caso_dir = Path(caso_dir)
    estado = valores.cargar_estado(caso_dir)
    fuentes = []
    est = valores.cargar_valores(caso_dir, "estaciones")
    if est is not None:
        for nombre in valores.estaciones_disponibles(est):
            fuentes.append(_fuente(nombre, valores.filtrar(est, nombre), "estaciones"))
    dom = valores.cargar_valores(caso_dir, "dominio")
    if dom is not None:
        fuentes.append(_fuente("Dominio (resumen)", valores.filtrar(dom), "dominio"))
    return {"objetivo": estado.get("dia_objetivo_local"), "fuentes": fuentes}


def construir_indice_html(caso_dir: Path, imagenes: List[Path]) -> str:
    """Página completa del ciclo: tabla interactiva + galería de mapas."""
    caso_dir = Path(caso_dir)
    datos = construir_datos(caso_dir)
    estado = valores.cargar_estado(caso_dir)
    sub = f"Ciclo {caso_dir.name[:10]} {caso_dir.name[11:13]}Z"
    if estado.get("t0"):
        sub += f" · asimilación hasta {estado['t0']} UTC"
    if estado.get("estaciones_asimiladas") is not None:
        sub += " · estaciones asimiladas: " + (", ".join(estado["estaciones_asimiladas"]) or "ninguna")
        if estado.get("estaciones_excluidas"):
            sub += " · sin datos suficientes: " + ", ".join(estado["estaciones_excluidas"])
    mapas = "".join(
        f"<figure><a href='{html.escape(p.name)}'><img src='{html.escape(p.name)}' alt='{html.escape(p.stem)}'></a>"
        f"<figcaption>{html.escape(p.stem.replace('producto_', ''))} UTC</figcaption></figure>"
        for p in imagenes)
    cuerpo = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    return (_PLANTILLA
            .replace("__TITULO__", html.escape(caso_dir.name))
            .replace("__SUBTITULO__", html.escape(sub))
            .replace("__OBJETIVO__", html.escape(str(datos["objetivo"] or "-")))
            .replace("__MAPAS__", mapas or "<p>Sin mapas.</p>")
            .replace("__DATOS__", cuerpo))
