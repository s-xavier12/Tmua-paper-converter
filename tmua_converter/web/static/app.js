"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
let pollTimer = null;
let beforeLeave = null;

function show(tplId) {
  view.innerHTML = "";
  view.appendChild($(tplId).content.cloneNode(true));
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok && res.status !== 422) {
    throw new Error((body && (body.detail || body.error)) || `${res.status} ${res.statusText}`);
  }
  return { status: res.status, body };
}

// ------------------------------------------------------------------ routing
function route() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "job" && parts[1] && parts[2] === "paper") return reviewView(parts[1], Number(parts[3] || 0));
  if (parts[0] === "job" && parts[1]) return jobView(parts[1]);
  return homeView();
}
window.addEventListener("hashchange", () => {
  if (beforeLeave && !beforeLeave()) return;
  beforeLeave = null;
  route();
});
window.addEventListener("beforeunload", (e) => { if (beforeLeave && !beforeLeave(true)) { e.preventDefault(); e.returnValue = ""; } });

// ------------------------------------------------------------------ home
async function homeView() {
  show("#tpl-home");
  const files = []; // {file, o: {title, paper, year, duration, questions}}
  const cfg = (await api("/api/config")).body;
  $("#ocr-note").textContent = cfg.ocr
    ? "Pictures (scans, photos, screenshots) are read with free offline OCR. OCR is weak on maths symbols, so every question read from a picture is marked for review."
    : "To convert pictures (scans, photos, screenshots), install the free Tesseract OCR program first - see the README. Digital PDFs work without it.";
  const isPic = (f) => !(f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));

  const list = $("#file-list");
  function renderFiles() {
    list.innerHTML = "";
    files.forEach((f, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="file-line"><span class="name">${esc(f.file.name)}</span>
        <button data-a="up" title="Move up">↑</button><button data-a="down" title="Move down">↓</button>
        <button data-a="rm" title="Remove">✕</button></div>
        <details><summary class="small">Optional overrides (leave blank to read them from the paper)</summary>
        <div class="overrides">
          <label>Title <input type="text" data-k="title" value="${esc(f.o.title)}"></label>
          <label>Paper <input type="text" data-k="paper" value="${esc(f.o.paper)}" placeholder="Paper 1"></label>
          <label>Year / set <input type="text" data-k="year" value="${esc(f.o.year)}"></label>
          <label>Minutes <input type="number" min="1" data-k="duration" value="${esc(f.o.duration)}"></label>
          <label>Questions <input type="number" min="1" data-k="questions" value="${esc(f.o.questions)}"></label>
        </div></details>`;
      li.addEventListener("click", (e) => {
        const a = e.target.dataset.a;
        if (!a) return;
        if (a === "rm") files.splice(i, 1);
        if (a === "up" && i > 0) [files[i - 1], files[i]] = [files[i], files[i - 1]];
        if (a === "down" && i < files.length - 1) [files[i + 1], files[i]] = [files[i], files[i + 1]];
        renderFiles();
      });
      li.addEventListener("input", (e) => { if (e.target.dataset.k) f.o[e.target.dataset.k] = e.target.value; });
      list.appendChild(li);
    });
    updateStart();
  }
  function addFiles(fl) {
    for (const f of fl) {
      const pdf = f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf");
      if (pdf || (f.type || "").startsWith("image/") || /\.(png|jpe?g|webp|tiff?|bmp)$/i.test(f.name)) {
        files.push({ file: f, o: { title: "", paper: "", year: "", duration: "", questions: "" } });
      }
    }
    renderFiles();
  }
  const drop = $("#drop");
  $("#pdfs").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));

  const combine = $("#combine");
  combine.addEventListener("change", () => { $("#combine-row").hidden = !combine.checked; updateStart(); });
  $("#combine-title").addEventListener("input", updateStart);
  $("#pages").addEventListener("change", updateStart);
  function paperCount() {
    const pics = files.filter((f) => isPic(f.file)).length;
    return files.length - pics + (pics && $("#pages").checked ? 1 : pics);
  }
  function updateStart() {
    $("#pages-row").hidden = files.filter((f) => isPic(f.file)).length < 2;
    let msg = "";
    if (!files.length) msg = "Add at least one PDF or picture.";
    else if (combine.checked && paperCount() < 2) msg = "Combining needs at least two papers.";
    else if (combine.checked && !$("#combine-title").value.trim()) msg = "Enter the combined paper's title.";
    $("#start").disabled = !!msg;
    $("#start-msg").textContent = msg;
  }

  $("#start").addEventListener("click", async () => {
    $("#start").disabled = true;
    $("#start-msg").textContent = "Uploading…";
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f.file, f.file.name));
    fd.append("options", JSON.stringify({
      combine_title: combine.checked ? $("#combine-title").value.trim() : "",
      images_are_pages: $("#pages").checked, papers: files.map((f) => f.o),
    }));
    try {
      const { body } = await api("/api/jobs", { method: "POST", body: fd });
      location.hash = `#/job/${body.id}`;
    } catch (err) {
      $("#start-msg").textContent = "Could not start: " + err.message;
      $("#start").disabled = false;
    }
  });

  $("#review-open").addEventListener("click", async () => {
    const jf = $("#review-json").files[0];
    if (!jf) { $("#review-msg").textContent = "Choose a .tmua.json file."; return; }
    const fd = new FormData();
    fd.append("file", jf, jf.name);
    const pf = $("#review-pdf").files[0];
    if (pf) fd.append("pdf", pf, pf.name);
    try {
      const { body } = await api("/api/review", { method: "POST", body: fd });
      location.hash = `#/job/${body.id}/paper/0`;
    } catch (err) { $("#review-msg").textContent = err.message; }
  });

  const jobs = (await api("/api/jobs")).body;
  if (jobs.length) {
    $("#recent-card").hidden = false;
    $("#recent").innerHTML = jobs.map((j) => {
      const names = j.papers.map((p) => p.file || p.pdf).filter(Boolean).join(", ");
      const href = j.kind === "review" ? `#/job/${j.id}/paper/0` : `#/job/${j.id}`;
      return `<li><a href="${href}">${esc(names || j.id)}</a> <span class="pill">${esc(j.status)}</span></li>`;
    }).join("");
  }
}

// ------------------------------------------------------------------ job progress
async function jobView(id) {
  show("#tpl-job");
  let logFrom = 0;
  const logEl = $("#log");
  async function tick() {
    let job;
    try { job = (await api(`/api/jobs/${id}?log_from=${logFrom}`)).body; } catch (err) {
      $("#job-title").textContent = "Job not found";
      return;
    }
    for (const l of job.log) logEl.textContent += `${String(l.t).padStart(6)}s  [${l.stage}] ${l.msg}\n`;
    logFrom = job.log_total;
    if (job.log.length) logEl.scrollTop = logEl.scrollHeight;
    const st = $("#job-status");
    st.textContent = job.status;
    st.className = "pill " + ({ done: "ok", failed: "err", running: "warn" }[job.status] || "");
    if (job.status === "done" || job.status === "failed") {
      $("#job-title").textContent = job.status === "done" ? "Conversion completed" : "Conversion failed";
      renderResults(job);
      return;
    }
    $("#job-title").textContent = "Converting…";
    pollTimer = setTimeout(tick, 2000);
  }
  tick();
}

function renderResults(job) {
  const box = $("#results");
  let html = "";
  if (job.error) html += `<p class="issues"><li class="error">${esc(job.error)}</li></p>`;
  for (const p of job.papers) {
    if (p.error || !p.summary) {
      html += `<div class="result"><h3>${esc(p.pdf)}</h3><ul class="issues"><li class="error">${esc(p.error || "not converted")}</li></ul></div>`;
      continue;
    }
    const s = p.summary;
    const v = s.validation;
    const flagged = Object.entries(s.needs_review || {});
    const pill = v.ok ? `<span class="pill ok">validated</span>` : `<span class="pill err">${v.errors} validation error(s)</span>`;
    html += `<div class="result">
      <h3>${esc(p.file)} ${pill}</h3>
      <ul>
        <li>${s.questions} questions · ${s.durationMinutes} minutes · from ${esc(p.pdf.replace(/^\d+_/, "").replace(".source.pdf", ""))}</li>
        <li>Every question checked: rebuilt from the page layout, cross-checked character by character against the PDF${s.render_check_used ? ", every expression rendered with KaTeX" : ""}</li>
        <li>Final file reloaded with a JSON parser and validated: ${v.errors} errors, ${v.warnings} warnings</li>
        <li>Questions with source images: ${s.questions_with_images.length ? s.questions_with_images.join(", ") : "none"}</li>
        <li>Needs review: ${flagged.length ? flagged.map(([n, r]) => `<br>Q${esc(n)} - ${esc(r.join("; "))}`).join("") : "none"}</li>
        ${(s.notes || []).map((n) => `<li class="muted">${esc(n)}</li>`).join("")}
      </ul>
      <div class="actions">
        <a class="button" href="#/job/${job.id}/paper/${p.index}">Review &amp; edit</a>
        <a class="button primary-link" href="/api/jobs/${job.id}/papers/${p.index}/download" download>Download .tmua.json</a>
        <a class="small" href="/api/jobs/${job.id}/papers/${p.index}/report" download>report</a>
      </div></div>`;
  }
  if (job.combined) {
    const r = job.combined_report || {};
    html += `<div class="result"><h3>${esc(job.combined)} <span class="pill ${r.ok ? "ok" : "err"}">${r.ok ? "validated" : (r.errors || 0) + " error(s)"}</span></h3>
      <p>Combined "${esc(job.combine_title)}" - papers in the order given, questions renumbered.</p>
      <div class="actions"><a class="button" href="/api/jobs/${job.id}/combined/download" download>Download combined .tmua.json</a></div></div>`;
  }
  box.innerHTML = html;
}

// ------------------------------------------------------------------ review / edit
async function reviewView(jobId, k) {
  show("#tpl-review");
  const base = `/api/jobs/${jobId}/papers/${k}`;
  let state;
  try { state = (await api(base)).body; } catch (err) {
    view.innerHTML = `<div class="card">Could not load the paper: ${esc(err.message)}</div>`;
    return;
  }
  let dirty = false;
  const setDirty = (d) => { dirty = d; $("#rv-dirty").hidden = !d; };
  beforeLeave = (quiet) => !dirty || (!quiet && confirm("Discard unsaved changes?"));
  $("#back").href = `#/job/${jobId}`;
  $("#rv-file").textContent = state.file;
  $("#rv-download").href = `${base}/download`;
  $("#rv-download").addEventListener("click", (e) => {
    if (dirty && !confirm("You have unsaved changes - the download is the last saved version. Continue?")) e.preventDefault();
  });

  const meta = $("#rv-meta");
  const fields = [["title", "Title"], ["paper", "Paper"], ["year", "Year / set"], ["durationMinutes", "Minutes"], ["id", "id"]];
  meta.innerHTML = fields.map(([f, l]) => `<label>${l}<input type="${f === "durationMinutes" ? "number" : "text"}" data-f="${f}"></label>`).join("");
  function fillMeta() { meta.querySelectorAll("input").forEach((i) => { i.value = state.paper[i.dataset.f] ?? ""; }); }
  fillMeta();
  meta.addEventListener("input", (e) => {
    const f = e.target.dataset.f;
    state.paper[f] = f === "durationMinutes" ? parseInt(e.target.value, 10) || 0 : e.target.value;
    setDirty(true);
  });

  const qBox = $("#rv-questions");
  let filter = "all";
  document.querySelectorAll("input[name=flt]").forEach((r) => r.addEventListener("change", () => { filter = r.value; renderAll(); }));

  function issuesFor(n) { return state.report.issues.filter((i) => i.question === n); }
  function renderReport() {
    const r = state.report;
    const paperIssues = r.issues.filter((i) => i.question == null);
    $("#rv-report").innerHTML = `<p>${r.ok ? '<span class="pill ok">validated: no errors</span>' : `<span class="pill err">${r.errors} error(s)</span>`}
      <span class="pill ${r.warnings ? "warn" : ""}">${r.warnings} warning(s)</span>
      <span class="muted small">${state.paper.questions.length} questions · ${state.paper.questions.filter((q) => q.needsReview).length} flagged for review</span></p>
      ${paperIssues.length ? `<ul class="issues">${paperIssues.map((i) => `<li class="${i.level}">${esc(i.message)}</li>`).join("")}</ul>` : ""}`;
  }

  function renderInto(el, q, errEl) {
    const errors = {};
    TMUA.renderQuestionInto(el, q, { onError: (f, m) => { errors[f] = m; } });
    const list = Object.entries(errors);
    errEl.innerHTML = list.map(([f, m]) => `<li class="error">${esc(f)}: ${esc(m)}</li>`).join("");
  }

  function card(q, idx) {
    const iss = issuesFor(q.number);
    const d = state.detail[String(q.number)] || {};
    const errs = iss.filter((i) => i.level === "error").length;
    const warns = iss.filter((i) => i.level === "warning").length;
    const el = document.createElement("section");
    el.className = "qcard" + (errs ? " has-error" : q.needsReview ? " flagged" : "");
    el.innerHTML = `
      <div class="qhead"><strong>Question ${q.number}</strong>
        <span>${q.needsReview ? '<span class="pill warn">needs review</span>' : ""}
          ${errs ? `<span class="pill err">${errs} error(s)</span>` : ""}${warns ? `<span class="pill warn">${warns} warning(s)</span>` : ""}
          <span class="pill">page ${q.sourcePage}</span>${q.images.length ? `<span class="pill">${q.images.length} image(s)</span>` : ""}
          <button data-a="edit">Edit</button></span></div>
      <div class="qcols">
        <div class="qcol"><h4>Source PDF</h4>${state.has_pdf
          ? `<img class="source-img" loading="lazy" alt="Question ${q.number} in the source PDF" src="${base}/source/${q.number}.png">`
          : '<p class="muted small">No PDF attached.</p>'}</div>
        <div class="qcol"><h4>As the simulator will show it</h4><div class="render-box"></div><ul class="issues katex-errs"></ul></div>
      </div>
      <div class="qfoot">
        <ul class="issues">${iss.map((i) => `<li class="${i.level}">${esc(i.field ? i.field + ": " : "")}${esc(i.message)}</li>`).join("")}</ul>
        ${(d.reviewReasons || []).length ? `<p class="small"><b>Review notes:</b> ${esc(d.reviewReasons.join("; "))}</p>` : ""}
        ${(d.history || []).length ? `<details><summary class="small">Check history</summary><ul class="history">${d.history.map((h) => `<li>${esc(h)}</li>`).join("")}</ul></details>` : ""}
        <div class="editor" hidden></div>
      </div>`;
    const renderBox = $(".render-box", el);
    const errBox = $(".katex-errs", el);
    renderInto(renderBox, q, errBox);

    const editor = $(".editor", el);
    $("[data-a=edit]", el).addEventListener("click", () => {
      if (!editor.hidden) { editor.hidden = true; return; }
      editor.hidden = false;
      editor.innerHTML = `
        <label>Stem (blank line = new paragraph)<textarea rows="${Math.min(14, 3 + q.stem.split("\n").length)}" data-e="stem">${esc(q.stem)}</textarea></label>
        ${q.options.map((o, i) => `<div class="opt"><b>${esc(o.label)}</b><textarea rows="1" data-e="opt" data-i="${i}">${esc(o.content)}</textarea></div>`).join("")}
        ${q.images.map((im, i) => `<div class="img-edit"><img src="${im.src}" alt=""><label>Alt text<input type="text" data-e="alt" data-i="${i}" value="${esc(im.alt)}"></label><button data-e="rmimg" data-i="${i}">Remove</button></div>`).join("")}
        <label class="check"><input type="checkbox" data-e="nr" ${q.needsReview ? "checked" : ""}> Needs review</label>`;
    });
    let t = null;
    editor.addEventListener("input", (e) => {
      const k = e.target.dataset.e;
      const i = Number(e.target.dataset.i);
      if (k === "stem") q.stem = e.target.value;
      else if (k === "opt") q.options[i].content = e.target.value;
      else if (k === "alt") q.images[i].alt = e.target.value;
      else if (k === "nr") q.needsReview = e.target.checked;
      setDirty(true);
      clearTimeout(t);
      t = setTimeout(() => renderInto(renderBox, q, errBox), 150);
    });
    editor.addEventListener("click", (e) => {
      if (e.target.dataset.e !== "rmimg") return;
      if (!confirm("Remove this image from the question?")) return;
      q.images.splice(Number(e.target.dataset.i), 1);
      setDirty(true);
      const fresh = card(q, idx);
      el.replaceWith(fresh);
      $("[data-a=edit]", fresh).click();
    });
    return el;
  }

  function renderAll() {
    renderReport();
    qBox.innerHTML = "";
    const qs = state.paper.questions.filter((q) => filter === "all" || q.needsReview || issuesFor(q.number).some((i) => i.level !== "info"));
    if (!qs.length) qBox.innerHTML = '<div class="card empty">Nothing flagged.</div>';
    qs.forEach((q, i) => qBox.appendChild(card(q, i)));
  }
  renderAll();

  $("#rv-save").addEventListener("click", async () => {
    $("#rv-save").disabled = true;
    try {
      const { status, body } = await api(base, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.paper) });
      if (status === 422) {
        alert("Not saved - the file structure is invalid:\n" + body.schema_errors.join("\n"));
      } else {
        state.paper = body.paper;
        state.report = body.report;
        setDirty(false);
        fillMeta();
        renderAll();
      }
    } catch (err) { alert("Save failed: " + err.message); }
    $("#rv-save").disabled = false;
  });
}

route();
