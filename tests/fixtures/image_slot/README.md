Byte copies of qontinui-schemas `rust/tests/fixtures/image_slot/{valid,invalid}`
at `c415a432156823d0b31c5b976e4ef9014576aae9` — the commit `pyproject.toml` pins
for the generated `ImageSlotSpec` model. Each valid fixture pins its RFC 8785
content hash on a `# content-sha256:` header line; `tests/test_compile_specs.py`
checks studio's hash against every one. Re-copy them when the pin moves.
