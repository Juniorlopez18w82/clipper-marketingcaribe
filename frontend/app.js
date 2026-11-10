// MarketingCaribe Clipper — frontend (same origin, talks to /api/*)
const $ = (id) => document.getElementById(id);
const state = { clipsJob: null, transcript: null, selected: null, words: null };

async function pollJob(jobId, onDone) {
  while (true) {
    const r = await fetch(`/api/jobs/${jobId}`);
    const j = await r.json();
    $("status").textContent = `${j.message || ""} (${j.progress || 0}%)`;
    if (j.status === "done") return onDone(j);
    if (j.status === "error") throw new Error(j.error || "El job falló");
    await new Promise((r) => setTimeout(r, 2000));
  }
}

// ---------- Vista previa en vivo: refleja Configuración antes de exportar ----------
function updatePreview() {
  const enabled = $("frameEnabled").checked;
  const brand = $("brandText").value.trim();
  const color = $("frameColor").value;
  const width = Math.max(0, parseInt($("frameWidth").value || "14", 10));
  $("frameLabel").textContent = enabled ? "Con marco" : "Sin marco";
  // Con marco: marco + nombre. Sin marco: NI marco NI nombre.
  $("pvFrame").style.display = enabled ? "block" : "none";
  $("pvFrame").style.border = `${width}px solid ${color}`;
  $("pvBrand").style.display = enabled && brand ? "block" : "none";
  $("pvBrand").textContent = brand || "@marketingcaribe";
  $("pvBrand").style.color = color;
  const showSubs = $("showSubs").value === "1";
  $("pvSubs").style.display = showSubs ? "block" : "none";
  $("pvSubs").style.fontSize = Math.max(14, Math.round(parseInt($("subSize").value || "76", 10) / 3.2)) + "px";
  $("pvSubs").style.bottom = $("subPos").value === "top" ? "380px" : $("subPos").value === "middle" ? "220px" : "70px";
  // Desactivar el campo del nombre cuando no hay marco (no se va a dibujar).
  $("brandText").disabled = !enabled;
  $("frameColor").disabled = !enabled;
  $("frameWidth").disabled = !enabled;
}
["frameEnabled", "brandText", "frameColor", "frameWidth", "showSubs", "subSize", "subPos", "subStyle"]
  .forEach((id) => $(id).addEventListener("input", updatePreview));
updatePreview();

// ---------- Paso 1 ----------
$("btnInfo").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return alert("Pega un link de YouTube");
  $("status").textContent = "Leyendo el video…";
  const r = await fetch("/api/video-info", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
  const d = await r.json();
  if (!r.ok) return $("status").textContent = d.detail || "No se pudo leer el video";
  const mins = d.duration ? `${Math.floor(d.duration / 60)}:${String(d.duration % 60).padStart(2, "0")}` : "live";
  $("videoMeta").textContent = `${d.title} · ${d.uploader || ""} · ${mins}`;
  $("rangeEnd").value = Math.min(d.duration || 120, 300);
  $("status").textContent = "Video listo. Genera los clips.";
};

$("btnClips").onclick = async () => {
  const url = $("url").value.trim();
  $("btnClips").disabled = true;
  try {
    const r = await fetch("/api/clips", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, start: parseFloat($("rangeStart").value || 0), end: parseFloat($("rangeEnd").value || 120), model: $("model").value }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Error");
    const job = await pollJob(d.job_id, (j) => j);
    state.clipsJob = job;
    state.transcript = job.result.transcript;
    const box = $("candidates");
    box.innerHTML = "";
    if (!job.result.candidates.length) { box.innerHTML = "<p class='muted'>No se encontraron clips en ese rango. Prueba otro rango.</p>"; return; }
    job.result.candidates.forEach((c, i) => {
      const div = document.createElement("div");
      div.className = "clip";
      div.innerHTML = `<b>Clip ${i + 1} · ${c.abs_start}s – ${c.abs_end}s (${c.duration}s)</b> · ⭐ ${c.score}<br><small>${c.text.slice(0, 160)}…</small>`;
      div.onclick = () => selectClip(c, div);
      box.appendChild(div);
    });
    selectClip(job.result.candidates[0], box.firstChild);
  } catch (e) { $("status").textContent = "Error: " + e.message; }
  finally { $("btnClips").disabled = false; }
};

function clipWords(absStart, absEnd) {
  // Words relative to the downloaded section, then to the clip.
  const secStart = state.clipsJob.result.section_start;
  const out = [];
  for (const s of state.transcript.segments)
    for (const w of s.words)
      if (secStart + w.s >= absStart && secStart + w.s < absEnd)
        out.push({ w: w.w, s: +(secStart + w.s - absStart).toFixed(2), e: +(secStart + w.e - absStart).toFixed(2) });
  return out;
}

function selectClip(c, el) {
  document.querySelectorAll(".clip").forEach((x) => x.classList.remove("sel"));
  if (el) el.classList.add("sel");
  state.selected = c;
  state.words = clipWords(c.abs_start, c.abs_end);
  $("clipStart").value = c.abs_start;
  $("clipEnd").value = c.abs_end;
  $("subText").value = c.text;
  $("pvSubs").innerHTML = state.words.slice(0, 3).map((w, i) => i === 1 ? `<span>${w.w.trim().toUpperCase()}</span>` : w.w.trim().toUpperCase()).join(" ") || "SUBTÍTULOS";
}

// ---------- Paso 3: render ----------
$("btnRender").onclick = async () => {
  if (!state.clipsJob) return alert("Primero genera los clips y elige uno");
  const start = parseFloat($("clipStart").value), end = parseFloat($("clipEnd").value);
  if (!(end > start)) return alert("El fin debe ser mayor que el inicio");
  // Palabras editadas: reparte el texto corregido en el tiempo del clip.
  let edited = null;
  const editedText = $("subText").value.trim();
  if (editedText && editedText !== state.selected.text) {
    const toks = editedText.split(/\s+/);
    const dur = end - start, step = dur / toks.length;
    edited = toks.map((t, i) => ({ w: " " + t, s: +(i * step).toFixed(2), e: +((i + 1) * step).toFixed(2) }));
  }
  $("btnRender").disabled = true;
  try {
    const body = {
      job_id: state.clipsJob.id, start, end, model: $("model").value,
      frame_enabled: $("frameEnabled").checked,
      frame_color: $("frameColor").value,
      frame_width: parseInt($("frameWidth").value || "14", 10),
      brand_text: $("brandText").value,
      title_text: $("titleText").value,
      show_subtitles: $("showSubs").value === "1",
      subtitle_style: $("subStyle").value,
      subtitle_size: parseInt($("subSize").value || "76", 10),
      subtitle_position: $("subPos").value,
      edited_words: edited,
    };
    const r = await fetch("/api/render", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Error al renderizar");
    const job = await pollJob(d.job_id, (j) => j);
    const file = job.result.file;
    $("resultCard").hidden = false;
    $("result").src = `/api/download/${file}`;
    $("downloadLink").href = `/api/download/${file}`;
    $("downloadLink").download = file;
    $("status").textContent = `Listo: ${file} · ${job.result.output.width}×${job.result.output.height} · marco: ${job.result.frame_enabled ? "sí" : "no"}`;
    document.getElementById("resultCard").scrollIntoView({ behavior: "smooth" });
  } catch (e) { $("status").textContent = "Error: " + e.message; }
  finally { $("btnRender").disabled = false; }
};
