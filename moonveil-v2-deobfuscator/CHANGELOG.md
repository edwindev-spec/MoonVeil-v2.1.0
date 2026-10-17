# Cambios

## 0.3.0

- **Modo `trace`** (`--mode trace|auto`): para layouts que el perfil estático no conoce, ejecuta el programa en un entorno simbólico (sin APIs reales) y reescribe sus operaciones como Luau. Recupera el segundo sample (flujo aplanado) como `loadstring(game:HttpGet("..."))()`. Es opcional y el manifiesto marca `protected_program_executed: true`.
- **Diagnóstico de layout**: `analyze` y `decompile` explican en qué se diferencia un archivo (campos, nombres de método, huellas coincidentes) en lugar de solo «expected 35 unique names».
- El emisor admite nodos de indexación y tablas constantes; nuevas pruebas de punto fijo (traza → Luau → traza) con programas escritos a mano y aleatorios.
- Fixture adicional `tests/fixtures/moonveil-2.1.0-flattened.lua` (sample suministrado por el usuario).

## 0.2.0

### Recuperación
- Nuevo emisor **estructurado** (`moonveil/emit.py`): reconstruye llamadas anidadas, llamadas a métodos y globales simples para cualquier programa lineal admitido, no solo el patrón de siete instrucciones. El sample queda como `loadstring(game:HttpGet("..."))()`.
- Se conserva el orden de efectos de la VM. Se corrigió un caso real descubierto por el fuzzer: Luau evalúa `obj:método(args)` resolviendo el método *después* de los argumentos.
- `--style auto|structured|registers`; `auto` vuelve a registros si el estilo estructurado no es demostrable o `luau-compile` lo rechaza.
- El modelo de registros usa asignación múltiple para resultados fijos.

### Análisis
- El manifiesto incluye `indicators` (URL, globales, métodos, comportamientos heurísticos) y el CLI los resume, escapando las cadenas.

### Robustez y seguridad
- Subprocesos con límites de CPU, memoria (`--memory-limit`), tamaño de archivo, sesión propia con muerte del grupo completo al expirar y entorno depurado.
- Mensajes de error que nombran a `luau` o `luau-compile` y describen las señales.
- El tokenizador rechaza saltos de línea dentro de cadenas simples, como Lua.
- La validación de claves duplicadas del volcado pasó de O(n²) a O(n).

### CLI
- `-o` opcional (por defecto junto a la entrada) y `-o -` para la salida estándar; `--json`, `--quiet`.
- Una tubería cerrada (`| head`) ya no produce un error.

### Pruebas e infraestructura
- Harness de Luau compartido con un entorno que registra lecturas de globales, búsquedas de métodos, llamadas y retornos.
- Fuzzing diferencial contra los handlers originales (dos generadores).
- Pruebas del emisor, del runtime, de los indicadores y del CLI de extremo a extremo.
- CI para Python 3.9–3.13, con lint y una pasada de fuzzing más larga.

## 0.1.0
Versión inicial: perfil `table-dispatch-210`, ambos cifrados, artefactos y manifiesto.
