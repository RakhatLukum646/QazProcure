# Комплект QazProcure — Gemini

Текущий провайдер — Gemini, модель задаётся GEMINI_MODEL, ключ GOOGLE_API_KEY. Для Docker нужен secrets/google_api_key, для Kubernetes — одноимённое поле Secret. Локальная языковая модель в поставку не входит.

Предыдущие отчёты другого провайдера вынесены из комплекта в локальный data/historical_provider_evidence; их показатели не подтверждают текущую Gemini-версию. Новый статус реального вызова: gemini_smoke.json. Полный factual-прогон Gemini пока не завершён.

Сохраняются доказательства расчётов: regional_core_example.json и regional_independent_check.json, данные покрытия, quality_report.json и аудит недоступных планов. Регионально-временная аналитика не зависит от провайдера LLM.

Воспроизведение: `python -m app.acceptance --factual-only --provider gemini --factual-dataset submission/factual_holdout_current.json --factual-output submission/factual_gemini.json --workers 3`.

Длительный SLA ≤24 часа и живое развёртывание Kubernetes не подтверждены. Документы: ../docs/compliance.md, ../docs/architecture.md, ../docs/methodology.md, ../docs/risks.md.

Проверка Gemini от 24.10.2026: 12/12 конечных ответов, 64/64 размеченных факта, шесть типов запросов RU/KZ. Протокол: submission/gemini_current_smoke.json (внутри submission — gemini_current_smoke.json). 102 unit/regression-теста прошли. Docker с Gemini: запуск, реальная ограниченная загрузка OWS и сохранность после перезапуска проверены. Это не полный прогон 114 вопросов.
