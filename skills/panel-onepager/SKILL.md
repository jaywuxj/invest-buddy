---
name: panel-onepager
model: flash
quality: standard
max_tokens: 5000
reconcile_text: true
trigger: panel-onepager（每周五）/ panel-build --full
---
公司总览（2026-10-15 改版：6 章 + 程序数据速览）：单股一次调用，模型只产出各章正文，
章节 id/编号/标题由程序固定；「财务与估值速览」由页面用实时数据渲染，模型不写。
不写个人持仓（输入里已去掉，程序另有兜底清洗）；不写会每天变化的数字（价格/估值倍数/目标价/市值），
贵不贵用程序给的定性提示 hints 表达；财报数字必须来自注入数据（文本数字对账开启）。
