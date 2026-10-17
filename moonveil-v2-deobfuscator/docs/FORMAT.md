# Perfil `table-dispatch-210`

Calibrado con el archivo suministrado, SHA-256:

```text
f12f071c462afa45076651a2b06b1b51527222471e895cfb3ed69ab6155d7e17
```

## Envoltorio y captura

El envoltorio devuelve `({...}):l({nombres}, "aa", ...)`. Para cada índice `i` desde cero, el remapeador asigna el valor posicional `i + 1` al nombre de índice Lua `((100151 * i + 58863) % 35) + 1`. Los campos con nombre no consumen un índice posicional.

El bootstrap inicializa los decodificadores base85, XOR y LZ, construye el deserializador y llama al constructor protegido. La instrumentación sustituye ese constructor antes del bootstrap. La tabla de handlers se obtiene de la celda 9 de sus capturas y se etiqueta por identidad de función. Sus IDs privados no se suponen constantes.

El grafo serializa valores primitivos, bytes y referencias de tablas. Los registros `T` conservan tablas vacías; las referencias compartidas se mantienen sin copiar ni ejecutar instrucciones del programa.

## Prototipo

| Campo | Uso en este perfil |
| --- | --- |
| 28 | Tamaño inicial de registros |
| 23 | Número de parámetros |
| 25 | Array de instrucciones |
| 14, 19 | Tablas auxiliares; deben estar vacías para emitir este perfil sin cierres |
| 11 | Upvalues; debe ser cero |
| 15, 30 | Metadatos numéricos conservados en el grafo |

Cada instrucción original tiene los campos `1, 3, 4, 8, 9, 10, 12, 13, 16, 17, 20, 27, 29`. El campo 20 elige el handler; los offsets siguientes se admiten solo cuando las huellas de sus handlers coinciden.

## Operaciones

`R` es la tabla de registros. Los contadores `B` y `C` incluyen uno: `B=1` pasa cero argumentos y `C=1` descarta los resultados. El valor cero usa el extremo dinámico `top` o conserva todos los resultados.

| Operación | Operandos / efecto |
| --- | --- |
| `loadk` | `A = field12 - 19`; `R[A] = field4` |
| `getglobal` | `A = field12 - 36`; `R[A] = environment[field4]` |
| `self` | `A = field12 - 45`, `B = field8 - 63`; guarda el receptor en `R[A+1]` y el método en `R[A]` |
| `call` | `A = field12 - 59`, `B = field8 - 41`, `C = field9 - 4` |
| `call_return` | Llamada: `A = field12 - 38`, `B = field8 - 5`, `C = field9 - 60`; retorno: inicio `field13 - 7`, contador `field3 - 4` |
| `nop` | Sin efecto |

Las llamadas con `C=0` actualizan `top` y los registros correspondientes, sin borrar los registros por encima del nuevo extremo. Los resultados fijos sí escriben `nil` si faltan valores. La emisión usa `table.pack` y extremos explícitos de `table.unpack` para conservar los huecos y los resultados múltiples.

## Cifrado de cadenas

Ambos handlers reemplazan su propia instrucción por un NOP y modifican el campo 4 de una instrucción posterior. Los índices siguientes empiezan en cero.

- `xor_index`: destino `PC + field12 - 42`. Descifra `cipher[i] XOR key[i % #key] XOR (i % 256)`.
- `xor_reverse`: destino `PC + field12 - 12`. Descifra `cipher[i] XOR key[#key - 1 - (i % #key)]`.

La normalización recorre las instrucciones en orden, de modo que cada modificación es visible para los decodificadores posteriores. Se rechazan claves vacías, destinos inválidos y handlers desconocidos.

## Ampliar el soporte

Una variante nueva necesita un sample completo y calibración de sus handlers, helpers y layout. Antes de agregar huellas u offsets, las pruebas deben contrastarlos con la VM original usando valores sintéticos, incluidos argumentos y retornos vacíos/múltiples. Un handler desconocido no se convierte en un comentario ni se elimina silenciosamente.

## Emisión

`moonveil/emit.py` ofrece dos estilos. Ambos se comparan en las pruebas con los handlers originales.

### `registers`

Conserva la tabla de registros `r` y el extremo `top`. Para llamadas con resultados fijos usa una asignación múltiple (`r[1], r[2] = f(...)`, que rellena con `nil` igual que la VM) y `table.pack` solo cuando `C = 0`.

### `structured`

Un ejecutor simbólico recorre las instrucciones sin ejecutarlas. Cada lectura de global, búsqueda de método y llamada es un nodo con efectos; `loadk` es puro. Una expresión solo se integra en su consumidor cuando:

1. Tiene exactamente un uso y los nodos integrados son los últimos pendientes, en el mismo orden en que el texto los evaluará. Si no, se materializan como locales en el orden del programa.
2. Cualquier sentencia emitida materializa antes todo lo pendiente (el orden de efectos manda).
3. Una llamada con un único resultado que quede en última posición de una lista se envuelve en paréntesis (`f((g()))`), porque Luau expandiría todos sus retornos.
4. Un `C = 0` solo se consume mediante `B = 0` (o `return` abierto) cuando es la instrucción inmediatamente anterior y sus resultados empiezan encima del llamado; leer individualmente un registro posterior a una llamada abierta depende del número de resultados y hace que se rechace el estilo.
5. `receptor:método(args)` solo se usa si los argumentos no tienen efectos pendientes: **Luau resuelve el método después de evaluar los argumentos** (la VM lo resuelve antes), de modo que con argumentos que lean globales cambiaría el orden observable. En ese caso se emite `local receiver = ...; local m = receiver.nombre; m(receiver, ...)`.
6. Los locales generados nunca ocultan un global que el programa lee, y se rechaza el estilo si harían falta más de 150 locales o una profundidad de anidación mayor que 24.

Los globales con nombre válido se emiten sin más (`loadstring`); el resto usa `environment[...]` con `local environment = getfenv()`. Los parámetros no usados no se declaran.

`--style auto` usa `structured` y recurre a `registers` si el estilo estructurado se rechaza o si `luau-compile` no acepta su resultado.

## Modo `trace`

`moonveil/trace.py` genera un script de Luau que:

1. Compila el programa con `loadstring` a partir de un literal con **todos los bytes escapados** (`\xNN`), de modo que el texto de la entrada nunca puede salirse de su cadena ni ejecutarse en el entorno real del script.
2. Le asigna con `setfenv` un entorno cuyo `__index` devuelve únicamente librerías puras (`string`, `table`, `bit32`, `math`, `utf8`, `buffer`, funciones básicas y un subconjunto de `os` con `clock/time/date/difftime`) y, para cualquier otro nombre, un *marcador*. `getfenv` devuelve siempre ese entorno y `setfenv` es inerte.
3. Cada marcador tiene una metatabla bloqueada: `__index` y `__call` anotan un evento y devuelven otro marcador; cualquier cálculo o asignación detiene el trazado con un motivo.

Registro (líneas separadas por tabuladores, entre `MOONVEIL_TRACE_BEGIN` y `MOONVEIL_TRACE_END`):

| Registro | Significado |
| --- | --- |
| `G id nombre` | Lectura de un global (nombre en hexadecimal) |
| `I id base clave` | Indexación de un marcador |
| `C id función n arg...` | Llamada con `n` argumentos |
| `T id` / `E id clave valor` | Tabla constante pasada como argumento y sus entradas |
| `S`, `F`, `X` | Estado final, motivo de parada y error del programa |

Los átomos son `Z` (nil), `B0/B1`, `N<número>`, `S<hex>`, `P<id>` (marcador) y `T<id>` (tabla). `emit.emit_trace` convierte el registro en los mismos nodos que usa el emisor estructurado (con un tipo `index` y llamadas «naturales»), funde `obj.nombre(obj, ...)` en `obj:nombre(...)` cuando es seguro y aplica las mismas reglas de orden de efectos.
