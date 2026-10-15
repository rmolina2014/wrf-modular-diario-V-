# Diagramas (archify)

Esta carpeta guarda los **diagramas interactivos** del proyecto generados con la skill
[archify](https://github.com/tt-a1i/archify).

## Qué se guarda acá

Cada diagrama es un **archivo HTML autónomo** (se abre en cualquier navegador, sin
dependencias ni instalación). Ejemplos de lo que se puede documentar:

- `arquitectura.html` — componentes del sistema (ingesta, QC, GFS, WRF, salida).
- `flujo.html` — flujo de trabajo del pipeline diario.
- `secuencia.html` — cadena de llamadas (EcoWitt API → QC → OBS_DOMAIN101 → WPS → WRF).
- `estados.html` — ciclo de vida de una corrida (`EJECUTANDO` → `EJECUTADO`/`FALLIDO`/...).

> Los HTML son artefactos generados: se pueden regenerar desde su especificación JSON.
> No se editan a mano.

## Cómo generar/actualizar un diagrama

La skill archify se usa desde el agente (no requiere instalación en el proyecto).
Pedile algo como:

```
Usá archify para diagramar la arquitectura de este repositorio:
ingesta EcoWitt → QC → OBS_DOMAIN101, GFS → WPS → real.exe → wrf.exe nudged,
y el producto final. Guardá el HTML en docs/archify/.
```

Para validar y entregar el diagrama, archify usa su propio toolchain:

```bash
node <skills>/archify/bin/archify.mjs validate <tipo> <candidato.json> --quality showcase --json
node <skills>/archify/bin/archify.mjs deliver  <tipo> <candidato.json> docs/archify/<salida>.html --quality showcase --json
```

Tipos disponibles: `architecture`, `workflow`, `sequence`, `dataflow`, `lifecycle`.
