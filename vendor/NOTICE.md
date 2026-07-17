# Vendored third-party code

**pdf.js** (`pdf.min.js`, `pdf.worker.min.js`) — Copyright 2023 Mozilla Foundation,
licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).
Source: https://github.com/mozilla/pdf.js (version 3.11.174, `build/` output from the
`pdfjs-dist` npm package).

Vendored (rather than loaded from a CDN) so the PDF print feature in `index.html`
works fully offline once the page has been loaded once.
