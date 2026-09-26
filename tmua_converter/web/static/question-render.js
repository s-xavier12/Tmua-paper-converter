// Renders one .tmua.json question the way an exam simulator would:
// paragraphs split on blank lines, single newlines as line breaks, KaTeX for
// $...$ and $$...$$.  Shared by the review UI and the headless renderer.
(function () {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Split on blank lines, but never inside $$...$$ display maths.
  function paragraphs(text) {
    const parts = [];
    let buf = "";
    let inDisplay = false;
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const count = (line.match(/\$\$/g) || []).length;
      if (!inDisplay && line.trim() === "" && buf !== "") {
        parts.push(buf);
        buf = "";
        continue;
      }
      buf = buf === "" ? line : buf + "\n" + line;
      if (count % 2 === 1) inDisplay = !inDisplay;
    }
    if (buf.trim() !== "") parts.push(buf);
    return parts;
  }

  function richText(el, text, field, onError) {
    el.innerHTML = paragraphs(text)
      .map((p) => "<p>" + escapeHtml(p).replace(/\n/g, "<br>") + "</p>")
      .join("");
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: true,
      errorCallback: function (msg, err) {
        if (onError) onError(field, String(err && err.message ? err.message : msg));
      },
    });
    // auto-render silently leaves maths it cannot delimit (e.g. an unclosed
    // brace swallows the closing $) as plain text; report any leftovers.
    if (onError) {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (node.parentElement && node.parentElement.closest(".katex")) continue;
        const m = node.nodeValue.match(/\$|\\[A-Za-z]+/);
        if (m) {
          onError(field, "unrendered LaTeX left as raw text near '" +
            node.nodeValue.substr(Math.max(0, m.index - 10), 40) + "'");
          break;
        }
      }
    }
  }

  function renderQuestionInto(root, q, opts) {
    opts = opts || {};
    const onError = opts.onError;
    root.innerHTML = "";
    root.classList.add("tmua-q");

    const head = document.createElement("div");
    head.className = "tmua-qnum";
    head.textContent = String(q.number);
    root.appendChild(head);

    const body = document.createElement("div");
    body.className = "tmua-qbody";
    root.appendChild(body);

    const stem = document.createElement("div");
    stem.className = "tmua-stem";
    richText(stem, q.stem || "", "stem", onError);
    body.appendChild(stem);

    (q.images || []).forEach(function (img, i) {
      const fig = document.createElement("figure");
      fig.className = "tmua-fig";
      const im = document.createElement("img");
      im.src = img.src;
      im.alt = img.alt || "";
      im.dataset.index = String(i);
      fig.appendChild(im);
      body.appendChild(fig);
    });

    const list = document.createElement("div");
    list.className = "tmua-options";
    (q.options || []).forEach(function (o) {
      const row = document.createElement("div");
      row.className = "tmua-option";
      const lab = document.createElement("span");
      lab.className = "tmua-label";
      lab.textContent = o.label;
      const content = document.createElement("span");
      content.className = "tmua-content";
      richText(content, o.content || "", "option " + o.label, onError);
      row.appendChild(lab);
      row.appendChild(content);
      list.appendChild(row);
    });
    body.appendChild(list);
    return root;
  }

  window.TMUA = window.TMUA || {};
  window.TMUA.renderQuestionInto = renderQuestionInto;
  window.TMUA.paragraphs = paragraphs;
})();
