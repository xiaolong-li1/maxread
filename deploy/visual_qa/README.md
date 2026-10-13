# MaxRead visual QA worker

This worker can run locally or on a separately provisioned browser host as an
on-demand subprocess. It does not start or depend on the MaxRead listener.

## Contract

- Input: a public Feishu Docx URL and an output directory.
- Output: viewport PNGs, `report.json`, and the same compact JSON on stdout.
- Detection: invalid formulas, leaked TeX/Markdown syntax, block overlap,
  image overflow, excessive image whitespace, and abnormally blank viewports.
- Mutation: none. The local MaxRead process owns authenticated Feishu writes.
- Retention: the latest 60 run directories are kept.

The production runner always opens the live Feishu page with Playwright/CDP.
It does not call Feishu's asynchronous Docx-to-PDF export or wait for an export
ticket. It blocks only on concrete visible failures such as invalid-formula
labels, raw formatting text, block overlap, broken images, or abnormal blank
viewports. Persisted image/formula/table counts are telemetry, not acceptance
thresholds. Successful pipeline runs remove browser screenshots; failed runs
retain them for diagnosis. `maxread_pdf_qa.py` is retained only as a manual,
offline diagnostic utility and is not selected by `run_visual_qa.sh`.

The local coordinator applies at most two deterministic block patches per
document. Supported patches are malformed formula/raw-format blocks and image
width overflow. Every visual patch is followed by another browser pass. Browser
or SSH failure is fail-open so the document service remains available; a
confirmed high-severity visual finding blocks delivery.

## Browser-host layout

```text
~/.local/share/maxread-browser/
  browsers/
  libs/
  python/
  maxread_visual_qa.py
  run_visual_qa.sh
  runs/
```

For a same-machine install, set `MAXREAD_VISUAL_QA_ROOT` to this resource
directory, `MAXREAD_VISUAL_QA_PYTHON` to the MaxRead virtualenv, and
`PLAYWRIGHT_BROWSERS_PATH` to the Playwright browser cache. Keep visual QA
disabled until a public Feishu URL passes a manual smoke test.

Manual smoke test:

```bash
~/.local/share/maxread-browser/run_visual_qa.sh \
  --url "https://tenant.feishu.cn/docx/TOKEN" \
  --output-dir ~/.local/share/maxread-browser/runs/manual \
  --max-sections 12
```
