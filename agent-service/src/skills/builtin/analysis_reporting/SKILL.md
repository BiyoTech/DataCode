# Analysis Reporting Guide

How to present analytical findings as rich, image-and-text Markdown that the frontend can
render. Load this when you are about to report query results, especially when a chart would
help.

---

## Answer Structure (always)

1. **Conclusion first**: a short, direct answer to the question with the key numbers.
2. **Supporting data**: a compact Markdown table for the underlying rows. Aggregate large
   result sets before showing them — do not dump hundreds of raw rows.
3. **Visualization**: add a chart when the data benefits from one (see below).
4. **Context**: summarize numerical results with meaning — trends, comparisons, significance.

---

## Charts — `echarts` code blocks

When a chart helps, emit an ` ```echarts ` code block whose body is a SINGLE valid JSON
object (an ECharts `option`). The frontend renders any ` ```echarts ` block via ECharts.

Rules:
- The block MUST contain pure JSON only — no comments, no trailing commas, and no
  JavaScript functions/expressions (e.g. no `formatter: function(){}`).
- Choose the chart type by intent:
  - trends over time -> `line`
  - category comparison -> `bar`
  - part-to-whole -> `pie`
  - correlation -> `scatter`
- Keep series/labels readable; pre-aggregate so a chart shows a sensible number of points.

Example:

```echarts
{
  "xAxis": { "type": "category", "data": ["Jan", "Feb", "Mar"] },
  "yAxis": { "type": "value" },
  "series": [{ "type": "line", "data": [120, 200, 150] }]
}
```

---

## Diagrams — `mermaid` code blocks

For process / flow explanations (not quantitative data), you may use a ` ```mermaid ` block
instead of a chart.

---

## Do / Don't

- DO lead with the answer, then show the evidence.
- DO keep tables compact and pre-aggregated.
- DO validate that every `echarts` block is strict JSON before emitting it.
- DON'T embed JS functions, comments, or trailing commas in `echarts` JSON.
- DON'T paste large raw result sets — summarize or aggregate first.
