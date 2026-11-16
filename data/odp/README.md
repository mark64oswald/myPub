# Open Data Product (ODP) family seed data

- `odp_family.yaml`: the hand-checked spec map (spec concepts, aliases and
  edges) read by `scripts/seed_odp_family.py`.
- `odpv-1.1.yaml`: the Open Data Product Vocabulary 1.1, copied unchanged from
  the `open-data-products` 0.7.0 wheel (`open_data_products/odpv/data/v1_1/vocab/odpv.yaml`).
  Apache 2.0, Open Data Product Initiative (Linux Foundation); see the
  `license` block in the file. When ODPV ships a new version, add its file
  next to this one, point `odpv.file` at it, and run
  `seed_odp_family.py render-map`.
- `odcs-json-schema-v3.1.0.json`: the Open Data Contract Standard v3.1.0
  JSON Schema, copied unchanged from bitol-io/open-data-contract-standard
  (Apache 2.0). `mcp-servers/kb-mcp/odp_gen.py` checks every generated ODCS
  contract against it (needs `jsonschema`). Replace it when ODCS ships a new
  version and update `ODCS_SCHEMA_FILE` in `odp_gen.py`.
