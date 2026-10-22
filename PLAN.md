# Plan operativo WRF San Juan — demostrador local estilo SMN

Estado: `08e521c` + WP-1 y WP-2 implementados (tests 34/34 OK, 2026-10-22).
Objetivo: demostrador reproducible para tesis, con el esqueleto operativo del SMN
(cadencia, asimilación, verificación, monitoreo) adaptado a una PC de 10 núcleos.

## 1. Protocolo temporal (ya implementado en `08e521c`)

```
start = <fecha> <ciclo>Z     t0 = start + asim_hours     end = start + run_hours
  |======= nudging activo ===|------- pronóstico libre -------|
```

- Por defecto: ciclo 06Z, 12 h de asimilación, t0 = 18Z, horizonte hasta el fin del
  día local siguiente (45 h totales).
- Solo entran a `OBS_DOMAIN101` observaciones de `[start, t0]`; preflight 2.2.7 bloquea
  obs fuera de la ventana o `fdda_end != asim_hours*60`.
- `extraer_valores.py` agrega `lead_h` y `fase` (`asimilacion` / `pronostico_libre`).
- El commit eliminó el hold-out: por defecto se asimilan todas las estaciones.

## 2. Decisiones adoptadas

1. **4 ciclos/día** (00/06/12/18 UTC), 12 h de asimilación en todos para que los ciclos
   sean comparables. El día objetivo se ancla a la fecha del ciclo GFS: día local
   siguiente a `fecha`, no al `t0` (necesario para el ciclo 18Z).
2. **GFS 0.5°** en los 4 ciclos (~2,2–3,2 GB por ciclo, transitorio); se purga tras
   generar `met_em`. GFS 0.25° queda como opción para sensibilidad.
3. **Hold-out opt-in** (`--holdout`): las 3 estaciones `rol=evaluacion` no se asimilan y
   se usan para verificar. Las corridas diarias de tesis usan hold-out; el modo por
   defecto del repo (todas las estaciones) se conserva.
4. **Verificación diferida**: no se puede verificar al terminar la corrida porque las obs
   del día objetivo todavía no existen. El verificador corre al día siguiente (D+2 en
   UTC), descarga el histórico EcoWitt del período objetivo y une con el CSV del modelo.
5. **Líneas base**: persistencia (obs en t0) y climatología horaria EcoWitt (30–60 días).
   Sin lector GRIB ni dependencias nuevas.
6. **Reforecast de 30 días** con hold-out, guardando solo pares de verificación, scores y
   productos (se borran wrfout y GFS).

## 3. Brecha frente al SMN (para el capítulo de tesis)

| Componente SMN | Este demostrador |
|---|---|
| WRF 4.0, 4 km, 1000×1250, 45 niveles, tope 10 hPa | WRF 4.5, 15 km, 80×60, 35 niveles, tope 5 hPa |
| 4 ciclos/día, 72 h determinístico | 4 ciclos/día, ~39–57 h (día objetivo completo) |
| 4D-LETKF, 40 miembros, 10 fuentes de obs | Nudging superficial EcoWitt (5-min) |
| Ensamble multifísico de 20 miembros | 1 miembro; ensamble como trabajo futuro |
| Verificación diaria vs 101 estaciones + calibración | Verificación vs 3 estaciones hold-out + baselines |
| Operación en HPC con monitoreo y métricas | PC única, systemd, registro y healthcheck |

## 4. Paquetes de trabajo

### WP-1 · Cuatro ciclos y GFS 0.5° (completado)
- `--dia-objetivo` (fecha local) en `calcular_ventana()`; horizonte calculado hasta el fin
  de ese día (múltiplo de 3 h).
- `GFS_RES` (default `0p50`) en descarga, verificación de tamaño y nombres
  (`gfs.t<HH>z.pgrb2.0p50.f<FFF>`).
- `--holdout`: reinsertar el filtro `rol=evaluacion` en `OBS_DOMAIN101` y contar
  asimilables en los umbrales.
- Wrapper `scripts/run_ciclo.sh <HH>`: calcula la fecha GFS (si `HH` aún no ocurrió hoy,
  usa ayer) y pasa `--ciclo`, `--dia-objetivo fecha+1`, `--asim-hours 12`, `--holdout`.
- Timers systemd: 13:30, 19:30, 01:30 y 07:30 UTC (≈2 h después de cada t0).
- Purga de GFS por ciclo y retención (wrfout 72 h, met_em 7 días, productos 90 días).

### WP-2 · Verificación diferida (completado)
- `src/verificacion/pares.py`: CSV del modelo (horario, `lead_h`/`fase`) ↔ EcoWitt
  10-min; agregación horaria; viento vectorial; QC; punto de grilla más cercano.
- `src/verificacion/metricas.py`: bias, MAE, RMSE, r, error circular; por variable,
  estación, ciclo y plazo; IC bootstrap; n explícito.
- Pendiente: Tmin/Tmax con ventanas SMN (00–12Z / 12–00Z) y lluvia 24 h (requiere
  agregar `RAINC+RAINNC` a `extraer_valores.py`). Hoy Tmin/Tmax solo se informan con
  cobertura horaria ≥90 %.
- `scripts/verificar_diario.py --caso <dir>` + `verificacion/{metricas.csv,resumen.json,informe.md,figuras}`.
- Líneas base: persistencia y climatología horaria.
- Tests sintéticos de métricas y del join temporal.

### WP-3 · Reforecast de 30 días
- Driver sobre el archivo AWS: 4 ciclos × 30 días = 120 corridas con hold-out.
- Guarda pares de verificación y productos; borra wrfout/GFS por corrida.
- Estimación: ~40 h mononúcleo; ~10 h reales en 4–6 núcleos, en varias noches.

### WP-4 · Cierre
- `docs/plan_operativo_smn.md` (estándar SMN, brecha, decisiones y limitaciones).
- README, `.env.template`, tag de git y material de defensa.

## 5. Presupuesto en esta PC

- GFS 0.5°: 14–20 archivos/ciclo ≈ 2,2–3,2 GB transitorio; se borra tras `met_em`.
- met_em: ~60 MB/corrida. wrfout: ~350–420 MB/corrida (7 MB/archivo horario).
- Retenido: ~1,5 GB/día; con wrfout a 72 h ≈ 5 GB. Entra en 36 GB libres.
- Cómputo: 15 min (36 h) → 20–23 min (57 h) mononúcleo; ~80–90 min/día.
  Con `WRF_NP=4–6`, ~25–40 min/día (el dominio chico escala poco).
- Reforecast: guardar solo pares/CSV mantiene el disco bajo (<1 GB).

## 6. Criterios de aceptación

- 7 días × 4 ciclos con ≥95 % de éxito, sin fugas de disco ni GFS huérfano.
- Productos del día objetivo disponibles después de cada t0 (+2 h).
- Informe de verificación D+2 con n, scores por plazo/ciclo y baselines.
- Reforecast con ≥10.000 pares horarios y tablas de Tmin/Tmax/lluvia.
- Reproducibilidad: un comando por ciclo, tests verdes, tag y README actualizado.

## 7. Riesgos

- Deriva con la otra PC: **hacer `git pull` y correr tests antes de cada sesión**.
- Con 3 estaciones hold-out la significancia es limitada; el reforecast de 30 días la
  amplía, pero no reemplaza una red densa.
- EcoWitt sin escudo/altura estándar: documentar QC y sesgo de sitio.
- El ciclo 18Z tiene t0 a las 06Z del día siguiente (03 local); su pronóstico cubre el
  resto del día objetivo pero no la madrugada. Documentarlo al comparar ciclos.
- GFS 0.5° es más grueso que el 0.25° del SMN: limita la escala meso-β.
