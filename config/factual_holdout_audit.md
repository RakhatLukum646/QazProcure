# Factual acceptance dataset corrections

- Pre-correction dataset SHA-256: `c43962637bc1291856c08fc35f0bb76ef9b4c38589b1ccabfc5fb7bc3085263a`.
- The in-progress baseline report `data/factual_baseline.json` records the same dataset hash and was left unchanged.
- Correction: the two `summary_milk_3y_*` questions now ask for a summary, matching the annotated `summary` intent; the prior wording asked to compare. This is a dataset-label correction, not a model improvement.
- Added contract URL facts to Russian and Kazakh lot-search cases and Russian and Kazakh price-anomaly cases. Each URL is constructed from the independently sourced target Contract ID using the URL pattern in `app.analytics.evidence`; values and other source facts were not changed.
- This is a development acceptance dataset authored with knowledge of the current renderer and known snapshot. It is not blind or third-party certification.

- Post-baseline correction: made `price_anomaly_above_none_ru/kk` self-contained by spelling out ENSTRU `292040.100.000003` and KATO `711210000`; cases are executed independently. Request and numerical ground truth are unchanged. The baseline report remains unmodified.
- Post-baseline correction: made both quantity-anomaly questions state ENSTRU `105111.410.000000`; made all four Fair Price questions state year 2026; and made all four price-anomaly questions state the requested 2024–2026 period. Request fields and numerical ground truth are unchanged.

## Refresh, 19 October 2026 (data drift, not a correction)

The background worker kept ingesting peer data between when `price_anomaly_below_ru/kk` were first authored and this re-run: `n_peers` for the target cohort (BIN 020240002363, ЕНС ТРУ 292040.100.000003, КАТО 711210000) grew from 14 to 15, which shifts the weighted reference price and deviation percentage. `reference_price`, `deviation_pct`, and `n_peers` were refreshed to the current `analyze()` output for both cases (`value`/`answer_text` only; `request` and `source` text unchanged). This is expected per `docs/risks.md` ("Сохранённый результат анализа фиксируется на момент расчёта... повторный расчёт после обновления данных может дать другое число"), not a routing or calculation bug — the item/contract IDs and URL were already correct before and after. Lesson for future runs: any dataset with hardcoded numeric ground truth drifts as the live DB grows; regenerate close in time to when the acceptance check actually runs (see `scripts/gen_factual_dataset.py`, which computes fresh ground truth at generation time rather than reusing old numbers).
