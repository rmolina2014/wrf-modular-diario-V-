# Verificación — 2026-10-21_12z

- **t0:** 2026-10-21 12:00 UTC
- **Período verificado:** 2026-10-21 12:00 a 2026-10-23 00:00 UTC
- **Conjunto:** asimilado (5 estaciones)
- **Pares horarios:** 77

## Métricas globales

| Variable | n | Bias | MAE | RMSE | r |
|---|---:|---:|---:|---:|---:|
| T2m | 77 | -1.76 | 2.52 | 3.07 | 0.732 |
| RH2m | 77 | -4.87 | 9.75 | 12.04 | 0.555 |
| PSFC | 77 | -18.88 | 20.28 | 27.16 | 0.932 |
| Viento10m | 77 | 8.89 | 10.24 | 13.81 | 0.203 |
| DirViento10m | 77 | -16.71 | 39.80 | — | — |

## T2m por plazo

| Lead (h) | n | Bias | MAE | RMSE |
|---:|---:|---:|---:|---:|
| 0 | 5 | -1.16 | 2.45 | 2.68 |
| 1 | 4 | 0.12 | 1.64 | 2.11 |
| 2 | 5 | -0.35 | 1.47 | 1.77 |
| 3 | 5 | -0.37 | 1.58 | 1.69 |
| 4 | 5 | 0.53 | 1.38 | 1.68 |
| 15 | 4 | -1.02 | 1.85 | 2.21 |
| 16 | 4 | -1.32 | 2.22 | 2.60 |
| 17 | 4 | -1.54 | 2.61 | 3.03 |
| 18 | 4 | -1.84 | 2.28 | 2.94 |
| 19 | 4 | -2.26 | 2.26 | 2.97 |
| 20 | 4 | -2.46 | 2.46 | 2.84 |
| 21 | 4 | -2.33 | 2.33 | 2.77 |
| 22 | 4 | -2.50 | 2.50 | 2.71 |
| 23 | 4 | -2.56 | 2.56 | 3.32 |
| 24 | 5 | -2.76 | 2.99 | 3.32 |
| 25 | 4 | -3.30 | 3.78 | 4.12 |
| 26 | 4 | -3.80 | 4.61 | 4.96 |
| 27 | 4 | -3.85 | 5.02 | 5.36 |

## Líneas base (RMSE T2m)

| Lead (h) | Persistencia | Climatología | Modelo |
|---:|---:|---:|---:|
| 0 | 0.00 | — | 2.68 |
| 1 | 1.38 | — | 2.11 |
| 2 | 2.83 | — | 1.77 |
| 3 | 4.31 | — | 1.69 |
| 4 | 5.33 | — | 1.68 |
| 15 | 3.23 | — | 2.21 |
| 16 | 2.67 | — | 2.60 |
| 17 | 2.39 | — | 3.03 |
| 18 | 2.19 | — | 2.94 |
| 19 | 2.33 | — | 2.97 |
| 20 | 2.08 | — | 2.84 |
| 21 | 2.01 | — | 2.77 |
| 22 | 3.07 | — | 2.71 |
| 23 | 3.40 | — | 3.32 |
| 24 | 4.16 | — | 3.32 |
| 25 | 5.42 | — | 4.12 |
| 26 | 6.65 | — | 4.96 |
| 27 | 7.82 | — | 5.36 |

## Notas

- Verificación diferida: las observaciones del día objetivo se descargan después de la corrida.
- Viento observado y modelado a 10 m; la altura real del sensor EcoWitt puede diferir.
- Tmin/Tmax se informan solo si la obs cubre ≥90 % de las horas del pronóstico; el modelo usa salida horaria y las obs datos de 10 min.
- PSFC compara presión de superficie del punto de grilla con la presión absoluta de la estación; la diferencia de altura introduce un sesgo sistemático.
