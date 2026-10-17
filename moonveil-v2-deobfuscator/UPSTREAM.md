# Procedencia

- Guía solicitada: [edwindev-spec/MoonVeil](https://github.com/edwindev-spec/MoonVeil), revisión [`a948c4849a692f0af40d554708cac28b610c5115`](https://github.com/edwindev-spec/MoonVeil/tree/a948c4849a692f0af40d554708cac28b610c5115), bajo licencia MIT. Esa versión admite MoonVeil 1.4.5.
- Se reutilizó `tools/install_luau.py`, incluido su pin de Luau 0.722 y las sumas SHA-256 oficiales. Se conserva su aviso MIT en `LICENSE.txt`.
- La implementación de `moonveil/` es nueva. Aplica el enfoque de capturar los datos antes del despacho, validar las operaciones conocidas y conservar artefactos, adaptado a la VM 2.1.0 del archivo proporcionado.
- `tests/fixtures/moonveil-2.1.0.lua` es el sample suministrado por el usuario, conservado sin cambios. La licencia de la herramienta no atribuye derechos sobre el contenido de terceros que procese.
- Luau pertenece a sus autores y se distribuye bajo [su licencia MIT](https://github.com/luau-lang/luau/blob/0.722/LICENSE.txt). Sus binarios se descargan durante la instalación y no forman parte de este repositorio.
- `tests/fixtures/moonveil-2.1.0-flattened.lua` es un segundo sample suministrado por el usuario, conservado sin cambios; solo lo procesa el modo `trace`.
