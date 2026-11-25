# Informe de ejecución — ciclo 2026-11-25 00Z

**Comando:** `./scripts/run_ciclo.sh 00`
**Ejecutado:** 2026-11-25 17:40:02 UTC → 18:26:07 UTC (~46 min)
**Resultado:** **FALLIDO** (código de salida `1`, estado `FALLIDO`)
**Caso:** `outputs/2026-11-25_00z/`
**Log principal:** `logs/run_2026-11-25_20261125_174002.log`
**Log WPS:** `logs/wps_20261125_00.log`

> No se generó pronóstico: la pipeline se detuvo en la etapa WPS/met_em, antes de
> `real.exe`, `wrf.exe` y los productos. No hay `wrfout`, CSV de valores ni mapas.

## Configuración de la corrida

| Parámetro | Valor |
|---|---|
| Inicio del modelo | 2026-11-25 00Z |
| t0 (fin de asimilación) | 2026-11-25 12Z |
| Asimilación | 12 h (`obs_nudge_opt=1`) |
| Pronóstico libre planificado | 39 h |
| Duración total planificada | 51 h (f000…f051) |
| Día objetivo local | 2026-11-26 (UTC-3) |
| Hold-out | sí |
| Resolución GFS | 0p50, fuente AWS |
| Horas de borde esperadas | 18 archivos `met_em` |

## Observaciones (etapa completada correctamente)

- Histórico EcoWitt descargado para 2026-11-24 y 2026-11-25 (3266 registros fusionados).
- Registros válidos del día: **3085**; dentro de la ventana `[00Z, 12Z]`: **915** en 8 estaciones.
- Asimilados en `OBS_DOMAIN101`: **580** observaciones en **4 estaciones**:
  `CUESTA_Viento`, `ECOHUMUS`, `PUNTA_NEGRA`, `VALLE_FERTIL`.
- Excluidas por cobertura insuficiente (mínimo 72 registros): `INTA_POCITO` (4) y
  `LOS_PIONEROS` (0). `LOS_PIONEROS` devolvió `API code 40012: Invalid MAC` en todas
  las variables consultadas.
- Archivos generados: `input/obs_flat.json`, `input/littler_20261125_0000.txt`,
  `input/OBS_DOMAIN101`.

## Fallo: descarga GFS incompleta → metgrid incompleto

1. **GFS:** de los 18 pronósticos necesarios se descargaron 17. El archivo
   `gfs.t00z.pgrb2.0p50.f018` falló con:

   ```
   IncompleteRead(25324382 bytes read, 134261445 more expected)
   ```

   Quedó un archivo parcial de **25 165 824 bytes** en lugar de ~159 585 827 bytes.
   El descargador registró el error pero continuó con las horas siguientes y no
   reintentó `f018` dentro de la misma corrida.

2. **WPS:** `ungrib`/`metgrid` procesaron correctamente hasta `2026-11-25 15Z`.
   Al procesar `2026-11-25 18Z`, `metgrid.exe` terminó con:

   ```
   ERROR: Error in ext_pkg_write_field
   WPS DIARIO COMPLETADO (51h)
   ```

   El script WPS devolvió código 0, pero el `met_em` de las 18Z quedó truncado
   (**239 bytes**); los 6 tiempos anteriores (00Z–15Z) sí son válidos.

3. **Orquestador:** `propagar_metem()` copió solo **7/18** archivos al run dir y
   abortó con `met_em incompletos: 7/18 archivos propagados al run dir`, antes del
   namelist diario, el preflight, `real.exe` y `wrf.exe`.

**Archivos inválidos que quedaron en disco:**

- `wps_sanjuan/gfs_data/2026-11-25_00/gfs.t00z.pgrb2.0p50.f018` (parcial)
- `wps_sanjuan/met_em.d01.2026-11-25_18:00:00.nc` (239 bytes)
- `run_wrf/met_em.d01.2026-11-25_18:00:00.nc` (239 bytes)

## Acciones correctivas recomendadas

1. Borrar los tres archivos inválidos listados arriba.
2. Relanzar `./scripts/run_ciclo.sh 00`. Los CSV de observaciones ya existen y se
   reutilizarán; `descargar_gfs()` detecta por tamaño que `f018` está corrupto y lo
   vuelve a descargar antes de repetir WPS.
3. Robustecer el pipeline (no aplicado en esta corrida):
   - que `GFSDownloader` reintente o elimine descargas incompletas en lugar de
     dejar el archivo parcial;
   - que `propagar_metem()` valide tamaño/netCDF de cada `met_em` y no cuente solo
     por nombre de archivo;
   - que `run_wps_diario.sh` no reporte «COMPLETADO» si `metgrid.exe` emitió un
     error fatal.

## Estado final de la corrida

| Salida | Resultado |
|---|---|
| Observaciones / `OBS_DOMAIN101` | OK (580 obs, 4 estaciones) |
| GFS | Parcial (17/18; fallo transitorio de red en `f018`) |
| WPS / `met_em` | Incompleto (7/18) |
| Preflight | No ejecutado |
| `real.exe` / `wrf.exe` | No ejecutados |
| wrfout / CSV / mapas | 0 / no generados / no generados |
