# wrf-modular-diario

Pronóstico diario operativo WRF con asimilación de observaciones EcoWitt (obs nudging) para la provincia de San Juan, Argentina. **v1: ejecución manual** por el operador; la automatización (systemd timer) es etapa 2.

## Objetivo

Todos los días se ejecuta el proceso completo para pronosticar las variables meteorológicas del **día posterior** a la fecha de corrida:

- Ciclo **12Z** de hoy, pronóstico de **36 h** (12Z hoy → 00Z pasado mañana): cubre todo el día de mañana.
- Asimilación por **observational nudging** durante todo el pronóstico (`obs_nudge_opt=1`, `fdda_end=2160`), solo corrida **nudged** (sin control).
- Variables del producto: **T2 (C), RH (%), PSFC (hPa), viento a 10 m**.

## Estructura

```
wrf-modular-diario/
├── run_diario.sh                 # Wrapper manual (loading .env + log)
├── scripts/
│   ├── pipeline_diario.py        # Orquestador del flujo diario
│   ├── run_wps_diario.sh         # WPS: ungrib + metgrid (12Z→+36h)
│   └── generar_producto.py       # Mapas de pronóstico por tiempo válido
├── config/
│   ├── estaciones.json           # Catálogo de estaciones EcoWitt
│   └── preflight_config.json     # Umbrales de chequeos previos a WRF
├── src/                          # Flujo modular (ingesta, calidad, asimilación, modelo, preflight)
├── namelist.input                # Base: run_hours=36, obs_nudge_opt=1
├── wps_sanjuan/                  # Trabajo WPS (geo_em estático + gfs_data + met_em)
├── data/raw/                     # Históricos EcoWitt del día (NO versionado)
├── logs/                         # Logs de cada corrida (NO versionado)
├── outputs/                      # Productos (NO versionado)
│   └── YYYY-MM-DD_12z/
│       ├── input/                # obs_flat, littler, OBS_DOMAIN101, namelist_diario
│       ├── nudged/               # wrfouts
│       ├── productos/            # mapas + indice.html
│       ├── preflight_diario.json
│       ├── informe_diario.md
│       └── estado.json
└── deploy/systemd/               # Etapa 2 (service + timer, no instalados)
```

## Configuración inicial

1. Copiar `.env.template` a `.env` y completar:
   - `ECOWITT_APP_KEY` / `ECOWITT_API_KEY` (y las de ECOHUMUS si corresponde).
   - Rutas WRF locales (`LOCAL_WRF_DIR`, `WPS_DIR`, `WRF_EJECUTABLES`, `GEOG_DATA_PATH`, `MPIRUN`, `WRF_NP`, `VALIDATION_PYTHON`).

El `.env` **no se sube al repositorio**. Las claves se leen desde el entorno la primera ejecución. El host debe operar en **UTC** (`TZ=UTC`, ya fijado por `run_diario.sh`): WRF y las observaciones trabajan en UTC.

2. Verificar que exista el entorno Python con las dependencias (`xarray`, `netCDF4`, `pandas`, `python-dotenv`, `requests`, `matplotlib`). En esta PC: `/home/pgich/anaconda3/envs/wrf-operativo-p3/bin/python`.

## Uso manual (v1)

```bash
cd /home/pgich/wrf-modular-diario-V-

# 1) Comprobar disponibilidad del día (obs + GFS 12Z, sin correr nada pesado)
./run_diario.sh --dry-run

# 2) Corrida completa del día de hoy (12Z, 36h)
./run_diario.sh

# 3) Otras opciones
./run_diario.sh --fecha 2026-10-13            # fecha específica
./run_diario.sh --run-hours 24                # horizonte distinto
./run_diario.sh --continue-sin-obs            # GFS puro si faltan obs (degradado)
./run_diario.sh --skip-wrf                    # solo preparar material hasta OBS_DOMAIN101
./run_diario.sh --fecha 2026-10-13 --dry-run  # disponibilidad de otra fecha
```

### Horario recomendado

El ciclo GFS **12Z** está completo en AWS S3 aproximadamente a las **17:30–18:30 UTC** (14:30–15:30 ART). Ejecutar la corrida después de esa hora; el modelo termina de noche (el wrapper sugiere ~19:30 UTC / 16:30 ART para la fase automática).

## Flujo del orquestador (`pipeline_diario.py`)

1. **Observaciones:** histórico EcoWitt del día → QC → `obs_flat.json`, `littler`, `OBS_DOMAIN101`.
   - Si la cobertura **asimilable en la ventana del ciclo** (estaciones con `rol != "evaluacion"`) no alcanza los umbrales (`OBS_MIN_REGISTROS=60`, `OBS_MIN_ESTACIONES=3`), se bloquea con estado `SIN_OBS` salvo `--continue-sin-obs`.
2. **GFS:** verifica los 13 horarios `f000..f036` del ciclo 12Z en AWS/NOMADS y los descarga.
3. **WPS:** `run_wps_diario.sh` (geogrid solo si falta `geo_em`) → `met_em` → copia al run dir WRF.
4. **Namelist diario:** `start=<fecha> 12Z`, `run_hours=36`, `obs_nudge_opt=1`, `fdda_end=2160`.
5. **Preflight** (bloqueante, sección 2.2) → **real.exe** → **wrf.exe nudged** (reintentos 5).
6. **Producto:** mapas T2/RH/PSFC/viento para los tiempos válidos de mañana + `indice.html` + `informe_diario.md` + `estado.json`.

## Estados posibles (`estado.json`)

`EJECUTADO`, `DRY_RUN`, `PREPARADO`, `SIN_OBS` (bloqueado), `SIN_GFS` (bloqueado), `BLOQUEADO` (preflight o corrida concurrente), `FALLIDO`.

El pipeline toma un **bloqueo** (`logs/corrida.lock`) para no pisar el run dir WRF: si ya hay una corrida en curso, la segunda sale con código 2 y estado `BLOQUEADO`. Un bloqueo obsoleto (proceso muerto o > 6 h) se elimina automáticamente.

## Automatización (etapa 2)

Los archivos `deploy/systemd/wrf-diario.{service,timer}` están listos pero **no instalados**. Para activarlos:

```bash
sudo cp deploy/systemd/wrf-diario.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wrf-diario.timer
```

El timer dispara a las **19:30 UTC** diarias y `Persistent=true` recupera corridas perdidas al volver el equipo. El servicio no recibe fecha: `run_diario.sh` usa **hoy UTC** (`date -u`); para forzar otra fecha, ejecutalo a mano con `--fecha YYYY-MM-DD`.