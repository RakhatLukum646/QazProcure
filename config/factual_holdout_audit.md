# Factual acceptance dataset corrections

- Pre-correction dataset SHA-256: `c43962637bc1291856c08fc35f0bb76ef9b4c38589b1ccabfc5fb7bc3085263a`.
- The in-progress baseline report `data/factual_baseline.json` records the same dataset hash and was left unchanged.
- Correction: the two `summary_milk_3y_*` questions now ask for a summary, matching the annotated `summary` intent; the prior wording asked to compare. This is a dataset-label correction, not a model improvement.
- Added contract URL facts to Russian and Kazakh lot-search cases and Russian and Kazakh price-anomaly cases. Each URL is constructed from the independently sourced target Contract ID using the URL pattern in `app.analytics.evidence`; values and other source facts were not changed.
- This is a development acceptance dataset authored with knowledge of the current renderer and known snapshot. It is not blind or third-party certification.

- Post-baseline correction: made `price_anomaly_above_none_ru/kk` self-contained by spelling out ENSTRU `292040.100.000003` and KATO `711210000`; cases are executed independently. Request and numerical ground truth are unchanged. The baseline report remains unmodified.
- Post-baseline correction: made both quantity-anomaly questions state ENSTRU `105111.410.000000`; made all four Fair Price questions state year 2026; and made all four price-anomaly questions state the requested 2024–2026 period. Request fields and numerical ground truth are unchanged.
