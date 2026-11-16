# Open Data Product (ODP) family seed data

- `odp_family.yaml`: the hand-checked spec map (spec concepts, aliases and
  edges) read by `scripts/seed_odp_family.py`.
- `odpv-1.1.yaml`: the Open Data Product Vocabulary 1.1, copied unchanged from
  the `open-data-products` 0.7.0 wheel (`open_data_products/odpv/data/v1_1/vocab/odpv.yaml`).
  Apache 2.0, Open Data Product Initiative (Linux Foundation); see the
  `license` block in the file. When ODPV ships a new version, add its file
  next to this one, point `odpv.file` at it, and run
  `seed_odp_family.py render-map`.
