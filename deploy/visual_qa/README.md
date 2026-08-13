# MaxRead visual QA worker

This worker runs on `ziplab-5090` as an on-demand SSH subprocess. It does not
start or depend on the stale remote MaxRead services.

## Contract

- Input: a public Feishu Docx URL and an output directory.
- Output: viewport PNGs, `report.json`, and the same compact JSON on stdout.
- Detection: invalid formulas, leaked TeX/Markdown syntax, block overlap,
  image overflow, excessive image whitespace, and abnormally blank viewports.
- Mutation: none. The local MaxRead process owns authenticated Feishu writes.
- Retention: the latest 60 run directories are kept.

The local coordinator applies at most two deterministic block patches per
document. Supported patches are malformed formula/raw-format blocks and image
width overflow. Every visual patch is followed by another browser pass. Browser
or SSH failure is fail-open so the document service remains available; a
confirmed high-severity visual finding blocks delivery.

## Remote layout

```text
~/.local/share/maxread-browser/
  browsers/
  libs/
  python/
  maxread_visual_qa.py
  run_visual_qa.sh
  runs/
```

Manual smoke test:

```bash
~/.local/share/maxread-browser/run_visual_qa.sh \
  --url "https://tenant.feishu.cn/docx/TOKEN" \
  --output-dir ~/.local/share/maxread-browser/runs/manual \
  --max-sections 12
```
