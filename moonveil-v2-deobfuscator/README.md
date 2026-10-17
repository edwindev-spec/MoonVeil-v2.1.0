# MoonVeil 2.1.0 Deobfuscator

Recupera código Luau de la **familia de VM por tabla del sample de MoonVeil 2.1.0**. Implementación nueva, guiada por [edwindev-spec/MoonVeil](https://github.com/edwindev-spec/MoonVeil), cuyo deobfuscador corresponde a 1.4.5.

Incluye el sample proporcionado en `tests/fixtures/moonveil-2.1.0.lua`. Recupera sus 11 instrucciones, descifra sus cadenas y lo reconstruye como la línea original:

```lua
loadstring(game:HttpGet("https://pastefy.app/DBk446XH/raw"))()
```

La URL es un dato de la entrada: la herramienta no descarga su contenido. Además informa de indicadores estáticos (URL, globales, métodos y comportamientos como «cargador remoto de código») sin ejecutar el programa protegido.

## Instalación

Requiere Python **3.9 o posterior** y el runtime oficial de Luau. No necesita dependencias Python para funcionar.

```sh
python tools/install_luau.py
python -m moonveil --version
```

El instalador verifica el SHA-256 de **Luau 0.722** e instala `luau` y `luau-compile` en `.tools/luau-0.722`. Admite Linux/Windows x86-64 y el paquete oficial de macOS. Para otras arquitecturas, proporciona una compilación oficial de Luau.

Instalación opcional del comando `moonveil`:

```sh
python -m pip install -e .
```

También puedes indicar `--luau /ruta/luau`, configurar `LUAU_PATH` o usar `PATH`. El compilador se busca junto al runtime, en `LUAU_COMPILE_PATH` y en `PATH`. Las rutas explícitas inválidas producen un error.

## Uso

```sh
# Analizar sin ejecutar Lua ni necesitar el runtime.
python -m moonveil analyze entrada.lua --json

# Recuperar código (escribe entrada.recovered.luau) y exigir que el compilador oficial lo acepte.
python -m moonveil decompile entrada.lua --require-compile

# Elegir la salida, o enviarla a la salida estándar con -o - (el estado va a stderr).
python -m moonveil decompile entrada.lua -o recuperado.luau
python -m moonveil decompile entrada.lua -o - > recuperado.luau

# Conservar el grafo, las instrucciones normalizadas y el manifiesto.
python -m moonveil decompile entrada.lua -o recuperado.luau --artifacts artefactos

# Manifiesto (con indicadores) como JSON para otras herramientas.
python -m moonveil decompile entrada.lua -o recuperado.luau --json

# Extraer únicamente los datos decodificados.
python -m moonveil dump entrada.lua -o decoded.json
```

Ejemplo con el sample:

```text
$ python -m moonveil decompile tests/fixtures/moonveil-2.1.0.lua -o recuperado.luau --require-compile
Recovered 11 instructions: recuperado.luau (structured, compiled with official Luau)
  behavior: remote-code-loader - downloads text and passes it to loadstring/load (heuristic: never verified by running it)
  url: "https://pastefy.app/DBk446XH/raw"  (data only; this tool never contacts it)
  globals: "loadstring", "game"; methods: "HttpGet"
```

| Opción | Efecto |
| --- | --- |
| `-o, --output` | Destino; `-` usa la salida estándar. Por defecto `<entrada>.recovered.luau` / `<entrada>.decoded.json`. |
| `--style auto\|structured\|registers` | `auto` (por defecto) emite Luau idiomático cuando puede demostrarse y, si no, el modelo de registros. Si `luau-compile` rechaza la versión estructurada, `auto` vuelve a registros y lo anota en el manifiesto. |
| `--timeout 30` | Límite **por subproceso**; exige un número positivo y finito. |
| `--memory-limit 1024` | Límite de espacio de direcciones por subproceso, en MiB (solo POSIX); `0` lo desactiva. |
| `--json` | Imprime el manifiesto en JSON en lugar del resumen de texto. |
| `-q, --quiet` | Solo errores. |

La entrada tiene un máximo de 8 MiB y la salida de cada proceso, 16 MiB. Los errores de entrada, perfil, runtime o compilación terminan con código **2**; éxito, **0**; interrupción, **130**.

La herramienta protege la entrada frente a alias y enlaces duros. Cada archivo se sustituye atómicamente después del procesamiento y la comprobación sintáctica; el conjunto de artefactos no es una transacción.

## Cómo funciona

1. Reconoce el envoltorio y su permutación de 35 campos mediante tokens, respetando comentarios y literales.
2. Identifica los handlers y helpers por huellas estructurales calibradas: no basta con cambiar el banner a `2.1.0`.
3. Sustituye el constructor protegido, ejecuta el bootstrap/deserializador con Luau y captura el grafo antes del primer despacho del programa.
4. Normaliza los dos cifrados de cadenas, cargas de constantes/globales, métodos, llamadas y retornos.
5. Eleva el programa a Luau sin el dispatcher (`moonveil/emit.py`). El estilo **estructurado** reconstruye llamadas anidadas, llamadas a métodos (`game:HttpGet(url)`) y globales simples, y solo integra una expresión en otra cuando se conserva el orden de evaluación de la VM, la regla de uno/varios resultados y el extremo abierto `top`. Los casos delicados (por ejemplo, registros obsoletos tras una llamada abierta, nombres no válidos o demasiados locales) pasan automáticamente al estilo de **registros**, que modela cualquier programa admitido.

Detalles del formato y sus offsets en [docs/FORMAT.md](docs/FORMAT.md).

## Artefactos y límites

Con `--artifacts` se generan:

| Archivo | Contenido |
| --- | --- |
| `decoded.json` | Grafo original, incluidos bytes cifrados y tablas vacías |
| `normalized.json` | Instrucciones normalizadas y cadenas en hexadecimal; texto UTF-8 cuando es válido |
| `instructions.txt` | Desensamblado con PC desde 1 y operaciones identificadas |
| `manifest.json` | Hashes de entrada/salida, perfil, estilo de emisión, recuentos, estado de compilación e indicadores |

El soporte está calibrado para el layout y los handlers del sample. **No se garantiza cualquier archivo marcado 2.1.0**: layouts, offsets o handlers diferentes se rechazan con un error. El perfil actual no tiene handlers de cierres, upvalues, saltos o bucles del programa protegido; los prototipos anidados se rechazan. Tampoco admite 1.4.5.

No se recuperan comentarios ni nombres originales. `compiled: true` significa que el compilador aceptó la salida; `compiled: null` indica que no estaba disponible. Usa `--require-compile` para exigirlo. El manifiesto deja `equivalent: null`: la CLI no ejecuta el programa recuperado para comparar su comportamiento.

El bootstrap y el deserializador ejecutan código procedente de la entrada con las APIs de la CLI oficial de Luau. Cada subproceso recibe contención de recursos: tiempo de pared y de CPU, espacio de direcciones, tamaño de salida y archivos, sesión propia (se mata todo el grupo al expirar) y un entorno sin variables heredadas salvo `PATH`. En Windows solo se aplican el tiempo y la salida. **Es contención de recursos, no un sandbox**: no aísla el sistema de archivos ni la red. Usa un entorno desechable para entradas desconocidas.

Los indicadores son heurísticos y estáticos: se leen de las constantes decodificadas, nunca de ejecutar el programa recuperado, y las cadenas se imprimen escapadas porque proceden de una entrada no fiable.

## Pruebas

```sh
python -B -m unittest discover -s tests -v
```

Para exigir las pruebas con Luau en Linux/macOS:

```sh
MOONVEIL_REQUIRE_LUAU=1 python -B -m unittest discover -s tests -v
```

En PowerShell, configura primero `$env:MOONVEIL_REQUIRE_LUAU='1'`. Si las herramientas están en otra ubicación, configura `LUAU_PATH` y, si hace falta, `LUAU_COMPILE_PATH`.

Las pruebas comparan la salida con los handlers originales para ambos cifrados, llamadas con resultados múltiples, huecos `nil`, registros solapados y argumentos. El sample se ejecuta solo con `game:HttpGet` y `loadstring` sustituidos por funciones locales; no se accede a su URL.

**Fuzzing diferencial** (`tests/test_fuzz.py`): genera programas aleatorios, unos imitando a un compilador (llamadas anidadas, métodos, listas de argumentos abiertas) y otros de bajo nivel, los ejecuta con los handlers originales de la VM como oráculo y exige que ambos estilos de emisión produzcan exactamente la misma traza (lecturas de globales, búsquedas de métodos, llamadas, argumentos y retornos). Ajusta la intensidad con variables de entorno:

```sh
MOONVEIL_REQUIRE_LUAU=1 MOONVEIL_FUZZ_CASES=2000 MOONVEIL_FUZZ_SEED=1 python -B -m unittest discover -s tests -p test_fuzz.py
```

Licencia MIT para la herramienta. Procedencia y atribución en [UPSTREAM.md](UPSTREAM.md).
