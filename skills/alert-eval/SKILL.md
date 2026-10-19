---
name: alert-eval
model: flash
quality: standard
max_tokens: 1000
trigger: event(预警命中后，可选)
---
预警命中解读：规则命中本不需要 LLM；此 skill 仅在需要生成一段人话解读时调用（P1 接入）。
