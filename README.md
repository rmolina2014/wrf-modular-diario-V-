# wrf-modular-diario

Pronóstico diario operativo WRF con asimilación de observaciones EcoWitt (obs nudging) para la provincia de San Juan, Argentina. **v1: ejecución manual** por el operador; la automatización (systemd timer) es etapa 2.

## Objetivo

Todos los días se ejecuta el proceso completo para pronosticar las variables meteorológicas del **día local siguiente** (mañana, hora de San Juan, UTC-3):

- **Asimilación** por *observational nudging* (`obs_nudge_opt=1`) con las observaciones EcoWitt, **hasta t0**.
- **Pronóstico libre** desde t0: el modelo corre solo, sin observaciones, hasta el fin del día objetivo.
- Solo corrida **nudged** (sin control).
- Variables del producto: **T2 (C), RH (%), PSFC (hPa), viento a 10 m**.

## Protocolo temporal (asimilación + pronóstico libre)

Mismo esquema que `wrf-modular-V1`. Las observaciones solo existen hasta el momento de la corrida, así que el nudging solo puede corregir el pasado; lo que viene después es pronóstico libre.

```
 06Z (start)                   18Z (t0)                           03Z día+2
   |========= nudging ==========|----------- pronóstico libre -----------|
        asim-hours = 12 h                     horizon-h = 33 h
                                      día objetivo local: 03Z día+1 .. 03Z día+2
```

| Parámetro | Default | Cómo cambiarlo |
|---|---|---|
| Ciclo GFS / inicio del modelo | 06Z | `--ciclo` o `CICLO_GFS` en `.env` |
| Horas de asimilación | 12 h (t0 = 18Z) | `--asim-hours` o `ASIM_HOURS` |
| Horizonte libre | el necesario para llegar al fin del día local siguiente (33 h) | `--horizon-h` o `--run-hours` (total) |
| Huso del día objetivo | UTC-3 | `HUSO_LOCAL_H` |

- `fdda_end = asim_hours × 60` min: el nudging se apaga en t0.
- Solo entran a `OBS_DOMAIN101` las observaciones de `[start, t0]`; ninguna posterior a t0. Se asimilan todas las estaciones con datos (sin hold-out).
- `run_hours` debe ser múltiplo de 3 h (las condiciones de borde llegan cada 3 h).
- Los productos (mapas y CSV) cubren el día objetivo; en el CSV cada fila lleva `lead_h` y `fase` (`asimilacion` / `pronostico_libre`).

## Estructura

```
wrf-modular-diario/
├── run_diario.sh                 # Wrapper manual (loading .env + log)
├── scripts/
│   ├── pipeline_diario.py        # Orquestador del flujo diario
│   ├── run_wps_diario.sh         # WPS: ungrib + metgrid (start -> start+run_hours)
│   ├── extraer_valores.py        # CSV de valores del pronóstico (estaciones y dominio)
│   └── generar_producto.py       # Mapas de pronóstico por tiempo válido
├── config/
│   ├── estaciones.json           # Catálogo de estaciones EcoWitt
│   └── preflight_config.json     # Umbrales de chequeos previos a WRF
├── src/                          # Flujo modular (ingesta, calidad, asimilación, modelo, preflight)
├── tests/                        # Tests de la ventana temporal y del preflight (pytest)
├── namelist.input                # Base (fechas, run_hours y fdda_end se reescriben cada día)
├── wps_sanjuan/                  # Trabajo WPS (geo_em estático + gfs_data + met_em)
├── data/raw/                     # Históricos EcoWitt del día (versionado)
├── logs/                         # Logs de cada corrida (NO versionado)
├── outputs/                      # Productos
│   └── YYYY-MM-DD_<ciclo>z/
│       ├── input/                # obs_flat, littler, OBS_DOMAIN101, namelist_diario
│       ├── nudged/               # wrfouts (NO versionado)
│       ├── productos/            # mapas + indice.html
│       ├── valores_pronostico_estaciones.csv
│       ├── valores_pronostico_dominio.csv
│       ├── preflight_diario.json
│       ├── informe_diario.md
│       └── estado.json
└── deploy/systemd/               # Etapa 2 (service + timer, no instalados)
```

## Configuración inicial

1. Copiar `.env.template` a `.env` y completar:
   - `ECOWITT_APP_KEY` / `ECOWITT_API_KEY` (y las de ECOHUMUS si corresponde).
   - Rutas WRF locales (`LOCAL_WRF_DIR`, `WPS_DIR`, `WRF_EJECUTABLES`, `GEOG_DATA_PATH`, `MPIRUN`, `WRF_NP`, `VALIDATION_PYTHON`).
   - Opcional: `CICLO_GFS`, `ASIM_HOURS`, `HUSO_LOCAL_H` (ver Protocolo temporal).

El `.env` **no se sube al repositorio**. El host debe operar en **UTC** (`TZ=UTC`, ya fijado por `run_diario.sh`): WRF y las observaciones trabajan en UTC.

2. Verificar que exista el entorno Python con las dependencias (`xarray`, `netCDF4`, `pandas`, `python-dotenv`, `requests`, `matplotlib`). En esta PC: `/home/pgich/anaconda3/envs/wrf-operativo-p3/bin/python`.

## Uso manual (v1)

```bash
cd /home/pgich/wrf-modular-diario-V-

# 1) Comprobar disponibilidad (obs de la ventana + GFS del ciclo, sin correr nada pesado)
./run_diario.sh --dry-run

# 2) Corrida completa de hoy (ciclo 06Z, asimila hasta 18Z, pronostica mañana)
./run_diario.sh

# 3) Otras opciones
./run_diario.sh --fecha 2026-10-13            # fecha específica
./run_diario.sh --asim-hours 6 --ciclo 12     # otra ventana de asimilación / ciclo
./run_diario.sh --horizon-h 24                # horizonte libre fijo
./run_diario.sh --continue-sin-obs            # GFS puro si faltan obs (degradado)
./run_diario.sh --skip-wrf                    # solo preparar material hasta OBS_DOMAIN101
```

### Horario recomendado

La corrida necesita las observaciones hasta **t0 (18Z con los defaults)**, así que debe lanzarse **después de las 18Z**; el timer de las 19:30 UTC (16:30 ART) deja margen. Si el CSV de observaciones ya descargado no llega hasta t0 (por ejemplo, de una corrida manual temprana), se vuelve a descargar solo. Si se lanza antes de t0 el pipeline avisa y asimila menos observaciones.

## Flujo del orquestador (`pipeline_diario.py`)

1. **Observaciones:** histórico EcoWitt de la ventana `[start, t0]` → QC → `obs_flat.json`, `littler`, `OBS_DOMAIN101`.
   - Se asimilan **todas las estaciones que tengan datos** en la ventana (sin hold-out; el campo `rol` de `config/estaciones.json` ya no se usa en la ejecución diaria).
   - Si la cobertura en la ventana no alcanza los umbrales (`OBS_MIN_REGISTROS=60`, `OBS_MIN_ESTACIONES=3`), se bloquea con estado `SIN_OBS` salvo `--continue-sin-obs`. Sin ningún registro en la ventana se bloquea siempre.
2. **GFS:** verifica los horarios `f000..f<run_hours>` cada 3 h del ciclo y los descarga.
3. **WPS:** `run_wps_diario.sh` (geogrid solo si falta `geo_em`) → `met_em` → copia al run dir WRF.
4. **Namelist diario:** `start=<fecha> <ciclo>Z`, `run_hours`, `obs_nudge_opt=1`, `fdda_end=asim_hours*60`.
5. **Preflight** (bloqueante, sección 2.2) → **real.exe** → **wrf.exe nudged** (reintentos 5). El chequeo **2.2.7** bloquea la corrida si hay observaciones fuera de `[start, t0]` o si `fdda_end` no coincide con la ventana de asimilación.
6. **Producto:** CSV de valores + mapas T2/RH/PSFC/viento del día objetivo + `indice.html` + `informe_diario.md` + `estado.json`.

## Estados posibles (`estado.json`)

`EJECUTADO`, `DRY_RUN`, `PREPARADO`, `SIN_OBS` (bloqueado), `SIN_GFS` (bloqueado), `BLOQUEADO` (preflight o corrida concurrente), `FALLIDO`.

El pipeline toma un **bloqueo** (`logs/corrida.lock`) para no pisar el run dir WRF: si ya hay una corrida en curso, la segunda sale con código 2 y estado `BLOQUEADO`. Un bloqueo obsoleto (proceso muerto o > 6 h) se elimina automáticamente.

## Tests

```bash
python -I -m pytest tests/
```

Cubren el cálculo de la ventana, el filtrado de observaciones y el preflight 2.2.7. No ejecutan WRF ni usan la red.

## Automatización (etapa 2)

Los archivos `deploy/systemd/wrf-diario.{service,timer}` están listos pero **no instalados**. Para activarlos:

```bash
sudo cp deploy/systemd/wrf-diario.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wrf-diario.timer
```

El timer dispara a las **19:30 UTC** diarias y `Persistent=true` recupera corridas perdidas al volver el equipo. El servicio no recibe fecha: `run_diario.sh` usa **hoy UTC** (`date -u`); para forzar otra fecha, ejecutalo a mano con `--fecha YYYY-MM-DD`.
