// ClipViral — frontend (same origin, talks to /api/*)
const $ = (id) => document.getElementById(id);
const state = { clipsJob: null, transcript: null, selected: null, words: null, user: null };

// ---------- Mi cuenta: registro / login (JWT en localStorage) ----------
function authHeaders(extra = {}) {
  const t = localStorage.getItem("clipper_token");
  return t ? { ...extra, Authorization: `Bearer ${t}` } : extra;
}
async function authCall(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || "Error");
  return d;
}
// El panel es la app: al abrirse toma TODA la pantalla (body.panel-open
// oculta el header oscuro y todo el <main> con display:none !important —
// el atributo hidden solo NO bastaba, porque header{display:flex} y
// main{display:grid} lo anulaban). Al cerrarse vuelve la landing.
function openPanel() {
  $("panel").hidden = false;
  document.body.classList.add("panel-open");
  document.querySelector("main").hidden = true;
  document.querySelector("header").hidden = true;
  window.scrollTo(0, 0);
}
function closePanel() {
  $("panel").hidden = true;
  document.body.classList.remove("panel-open");
  document.querySelector("main").hidden = false;
  document.querySelector("header").hidden = false;
}
function updateTopBar() {
  const u = state.user;
  if (!u) return;
  // Barra superior: nombre real + insignia. El ADMIN siempre es "Admin"
  // (roja), aunque su campo plan sea "free"; un usuario normal sin plan
  // ve "Sin plan", y con plan ve el nombre de su plan.
  $("panelUserName").textContent = u.name || "";
  $("panelUserPlan").textContent = u.is_admin ? "Admin" : (u.plan && u.plan !== "free" ? u.plan : "Sin plan");
  $("panelUserPlan").classList.toggle("admin", !!u.is_admin);
}
function doLogout() {
  localStorage.removeItem("clipper_token");
  showUser(null);
}
function showUser(user) {
  state.user = user;
  $("authLoggedOut").hidden = !!user;
  $("authLoggedIn").hidden = !user;
  if (user) {
    $("authUserName").textContent = user.name;
    $("authUserEmail").textContent = user.email;
    $("authUserPlan").textContent = user.is_admin ? "Admin" : user.plan;
    updateTopBar();
    $("panelWelcome").textContent = `Hola, ${user.name} — tus clips, tus aprobaciones y tu cuenta, todo en un solo lugar.`;
    $("tabAdmin").hidden = !user.is_admin;
    // La configuración guardada del usuario manda en el creador.
    if (user.brand_text) $("brandText").value = user.brand_text;
    $("frameEnabled").checked = user.frame_enabled !== false;
    updatePreview();
    loadPanel();
    // Al entrar, el panel SE ABRE SOLO: el panel es la app, la landing
    // es solo la puerta.
    openPanel();
  } else {
    // Sin sesión: se cierra el panel y vuelve la landing.
    closePanel();
  }
}
async function refreshMe() {
  const t = localStorage.getItem("clipper_token");
  if (!t) return showUser(null);
  try {
    const r = await fetch("/api/auth/me", { headers: authHeaders() });
    if (!r.ok) throw new Error();
    showUser(await r.json());
  } catch { localStorage.removeItem("clipper_token"); showUser(null); }
}
$("btnLogin").onclick = async () => {
  try {
    const d = await authCall("/api/auth/login", { email: $("authEmail").value.trim(), password: $("authPass").value });
    localStorage.setItem("clipper_token", d.access_token);
    $("authMsg").textContent = "";
    showUser(d.user);
  } catch (e) { $("authMsg").textContent = "Error: " + e.message; }
};
$("btnRegister").onclick = async () => {
  try {
    const d = await authCall("/api/auth/register", { name: $("authName").value.trim(), email: $("authEmail").value.trim(), password: $("authPass").value });
    localStorage.setItem("clipper_token", d.access_token);
    $("authMsg").textContent = "";
    showUser(d.user);
  } catch (e) { $("authMsg").textContent = "Error: " + e.message; }
};
$("btnLogout").onclick = doLogout;
$("btnLogoutTop").onclick = doLogout;
$("btnShowPanel").onclick = () => openPanel();
refreshMe();

// ---------- Panel: pestañas ----------
document.querySelectorAll(".panel .tab[data-tab]").forEach((btn) => {
  btn.onclick = () => {
    if (btn.dataset.tab === "creator") { closePanel(); return; }
    document.querySelectorAll(".panel .tab[data-tab]").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".panel .pane").forEach((p) => p.hidden = true);
    $("pane-" + btn.dataset.tab).hidden = false;
    if (btn.dataset.tab === "clips") loadMyClips();
    if (btn.dataset.tab === "admin") loadAdmin();
  };
});

async function loadPanel() {
  // ¿Tiene plan activo? Si no, se bloquean Inicio / Mis Clips / Crear / Cuenta.
  // El ADMIN nunca se bloquea: no necesita plan para usar la plataforma.
  let hasPlan = !!(state.user && state.user.is_admin), activePlanId = null;
  try {
    const r = await fetch("/api/subscriptions/mine", { headers: authHeaders() });
    const d = await r.json();
    hasPlan = hasPlan || !!d.active_subscription;
    activePlanId = d.active_subscription ? d.active_subscription.plan_id : null;
    renderMyRequests(d.requests || []);
  } catch { /* sin plan */ }
  ["home", "clips", "create", "account"].forEach((t) => {
    const pane = $("pane-" + t);
    if (pane) pane.classList.toggle("is-locked", !hasPlan);
  });
  try {
    const r = await fetch("/api/me/stats", { headers: authHeaders() });
    const s = await r.json();
    $("stTotal").textContent = s.clips ?? 0;
    $("stPend").textContent = s.pendientes ?? 0;
    $("stApr").textContent = s.aprobados ?? 0;
  } catch { /* stats best-effort */ }
  $("accName").value = state.user.name || "";
  $("accEmail").value = state.user.email || "";
  $("accBrand").value = state.user.brand_text || "@marketingcaribe";
  $("accFrame").checked = state.user.frame_enabled !== false;
  renderPlans(activePlanId);
  loadMyClips();
  if (state.user.is_admin) loadAdmin();
}

function renderMyRequests(reqs) {
  const box = $("myRequests");
  if (!reqs.length) { box.innerHTML = ""; return; }
  box.innerHTML = "<h3 style='font-size:20px'>Mis solicitudes</h3>" + reqs.map((r) =>
    `<div class="rowline"><div><b>${r.plan_name || "Plan"}</b> · ${r.payment_method_name || ""}<br><small>${r.created_at ? r.created_at.slice(0, 10) : ""}</small></div><span class="badge ${r.status === "approved" ? "aprobado" : ""}">${r.status === "pending" ? "Pendiente" : r.status === "approved" ? "Aprobada" : "Rechazada"}</span></div>`).join("");
}

// ---------- Planes ----------
let _plans = [], _methods = [], _buyPlan = null;
function featureItem(f) {
  const no = f.trim().toLowerCase().startsWith("sin ");
  return `<li class="${no ? "no" : ""}">${no ? "✗" : "✓"} ${f}</li>`;
}
async function renderPlans(activePlanId) {
  const box = $("planCards");
  try {
    const r = await fetch("/api/plans");
    _plans = await r.json();
    box.innerHTML = "";
    _plans.forEach((p) => {
      const div = document.createElement("div");
      div.className = "plan" + (p.is_popular ? " popular" : "");
      const isMine = activePlanId === p.id;
      div.innerHTML = `${p.is_popular ? '<span class="tag">⭐ Más popular</span>' : ""} ${isMine ? '<span class="tag mine">✓ Tu plan actual</span>' : ""}
        <h4>${p.name}</h4>
        <div class="price">RD$ ${p.price_dop.toLocaleString("es-DO")} <small>/ ${p.duration_days} días</small></div>
        <ul>${(p.features || []).map(featureItem).join("")}</ul>
        <button data-plan="${p.id}">${isMine ? "Tu plan actual" : "Contratar"}</button>`;
      div.querySelector("button").onclick = () => startBuy(p);
      box.appendChild(div);
    });
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar los planes.</p>"; }
}
async function startBuy(plan) {
  _buyPlan = plan;
  $("buyBox").hidden = false;
  $("buyTitle").textContent = `Contratar ${plan.name} — RD$ ${plan.price_dop.toLocaleString("es-DO")}`;
  $("buyMsg").textContent = "";
  try {
    const r = await fetch("/api/payment-methods", { headers: authHeaders() });
    _methods = await r.json();
    const sel = $("buyMethod");
    sel.innerHTML = _methods.map((m) => `<option value="${m.id}">${m.name} — ${m.bank}</option>`).join("");
    if (!_methods.length) { sel.innerHTML = "<option value=''>No hay métodos de pago activos</option>"; }
    showBank();
  } catch { $("buyMsg").textContent = "No se pudieron cargar los métodos de pago."; }
  $("buyBox").scrollIntoView({ behavior: "smooth" });
}
function showBank() {
  const m = _methods.find((x) => String(x.id) === $("buyMethod").value);
  const box = $("bankBox");
  if (!m) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<b>Banco:</b> ${m.bank}<br><b>Cuenta (${m.account_type}):</b> ${m.account_number}<br><b>Titular:</b> ${m.account_holder}<br><b>Instrucciones:</b> ${m.instructions || "—"}`;
}
$("buyMethod").onchange = showBank;
$("btnBuy").onclick = async () => {
  const file = $("buyReceipt").files[0];
  if (!_buyPlan) return;
  if (!file) return $("buyMsg").textContent = "Sube el comprobante (imagen o PDF).";
  const fd = new FormData();
  fd.append("plan_id", _buyPlan.id);
  fd.append("payment_method_id", $("buyMethod").value);
  fd.append("receipt", file);
  try {
    const r = await fetch("/api/subscriptions", { method: "POST", headers: authHeaders(), body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Error");
    $("buyMsg").textContent = "Solicitud enviada. Queda Pendiente hasta que el admin la apruebe.";
    $("buyBox").hidden = true;
    loadPanel();
  } catch (e) { $("buyMsg").textContent = "Error: " + e.message; }
};

// ---------- Panel Admin: solicitudes, planes y métodos ----------
async function loadAdminRequests() {
  try {
    const r = await fetch("/api/admin/subscriptions", { headers: authHeaders() });
    const list = await r.json();
    const box = $("adminRequests");
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<p class='muted'>No hay solicitudes.</p>"; return; }
    list.forEach((s) => {
      const div = document.createElement("div");
      div.className = "rowline";
      div.innerHTML = `<div><b>${s.plan_name || "Plan"}</b> · ${s.user_name || ""} (${s.user_email || ""})<br><small>${s.payment_method_name || ""} · ${s.created_at ? s.created_at.slice(0, 10) : ""}</small><br><a href="${s.receipt_url}" target="_blank">Ver comprobante</a></div>
        <span class="badge ${s.status === "approved" ? "aprobado" : ""}">${s.status === "pending" ? "Pendiente" : s.status === "approved" ? "Aprobada" : "Rechazada"}</span>`;
      if (s.status === "pending") {
        const acts = document.createElement("div");
        acts.innerHTML = `<button class="small" data-act="approve">Aprobar</button> <button class="small black" data-act="reject">Rechazar</button>`;
        acts.querySelector('[data-act="approve"]').onclick = async () => { await fetch(`/api/admin/subscriptions/${s.id}/approve`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({}) }); loadAdminRequests(); loadPanel(); };
        acts.querySelector('[data-act="reject"]').onclick = async () => { await fetch(`/api/admin/subscriptions/${s.id}/reject`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({}) }); loadAdminRequests(); loadPanel(); };
        div.appendChild(acts);
      }
      box.appendChild(div);
    });
  } catch { $("adminRequests").innerHTML = "<p class='muted'>No se pudieron cargar las solicitudes.</p>"; }
}
async function loadAdminPlans() {
  try {
    const r = await fetch("/api/plans");
    const plans = await r.json();
    $("adminPlans").innerHTML = plans.map((p) =>
      `<div class="rowline"><div><b>${p.name}</b> · RD$ ${p.price_dop.toLocaleString("es-DO")} · ${p.duration_days} días ${p.is_popular ? "· ⭐ Popular" : ""}</div>
      <button class="small black" data-del="${p.id}">Eliminar</button></div>`).join("") || "<p class='muted'>No hay planes.</p>";
    $("adminPlans").querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { await fetch(`/api/admin/plans/${b.dataset.del}`, { method: "DELETE", headers: authHeaders() }); loadAdminPlans(); renderPlans(); });
  } catch { $("adminPlans").innerHTML = "<p class='muted'>No se pudieron cargar los planes.</p>"; }
}
async function loadAdminMethods() {
  try {
    const r = await fetch("/api/payment-methods", { headers: authHeaders() });
    const methods = await r.json();
    const box = $("adminMethods");
    box.innerHTML = "";
    // Los inactivos (ej. el ejemplo) no salen en /api/payment-methods: se listan aparte.
    methods.forEach((m) => {
      const div = document.createElement("div");
      div.className = "rowline";
      div.innerHTML = `<div><b>${m.name}</b> · ${m.bank} · ${m.account_number}<br><small>${m.account_holder} · ${m.account_type}</small></div>
        <button class="small black" data-del="${m.id}">Eliminar</button>`;
      div.querySelector("[data-del]").onclick = async () => { await fetch(`/api/admin/payment-methods/${m.id}`, { method: "DELETE", headers: authHeaders() }); loadAdminMethods(); };
      box.appendChild(div);
    });
    if (!methods.length) box.innerHTML = "<p class='muted'>No hay métodos activos. Crea uno abajo.</p>";
  } catch { $("adminMethods").innerHTML = "<p class='muted'>No se pudieron cargar los métodos.</p>"; }
}
$("btnNewPlan").onclick = async () => {
  const body = { name: $("npName").value.trim(), price_dop: parseInt($("npPrice").value || "0", 10), duration_days: parseInt($("npDays").value || "30", 10),
    features: $("npFeatures").value.split("\n").map((s) => s.trim()).filter(Boolean), is_popular: $("npPopular").checked, is_active: true, sort_order: 99 };
  if (!body.name) return alert("Ponle un nombre al plan");
  const r = await fetch("/api/admin/plans", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
  if (!r.ok) return alert("No se pudo crear el plan");
  $("npName").value = ""; $("npFeatures").value = "";
  loadAdminPlans(); renderPlans();
};
$("btnNewMethod").onclick = async () => {
  const body = { name: $("nmName").value.trim(), bank: $("nmBank").value.trim(), account_number: $("nmAccount").value.trim(),
    account_holder: $("nmHolder").value.trim(), account_type: $("nmType").value.trim(), instructions: $("nmInstr").value.trim(), is_active: true };
  if (!body.name) return alert("Ponle un nombre al método");
  const r = await fetch("/api/admin/payment-methods", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
  if (!r.ok) return alert("No se pudo crear el método");
  ["nmName", "nmBank", "nmAccount", "nmHolder", "nmType", "nmInstr"].forEach((id) => $(id).value = "");
  loadAdminMethods();
};

function clipRow(c, admin) {
  const div = document.createElement("div");
  div.className = "rowline";
  const dl = c.status === "aprobado" && c.download_url
    ? `<a href="${c.download_url}" download><button class="small">Descargar MP4</button></a>`
    : `<span class="muted">${c.status === "pendiente" ? "Esperando aprobación del admin" : "Rechazado"}</span>`;
  const who = admin ? `<br><small>${c.user_name || ""} · ${c.user_email || ""}</small>` : "";
  div.innerHTML = `<div><b>${(c.title || "Clip").slice(0, 60) || "Clip"}</b> · ${c.start}s – ${c.end}s<br><small>${(c.video_url || "").slice(0, 70)}</small>${who}</div>
    <div><span class="badge ${c.status}">${c.status}</span> ${dl}</div>`;
  return div;
}

async function loadMyClips() {
  const box = $("myClips");
  try {
    const r = await fetch("/api/me/clips", { headers: authHeaders() });
    const list = await r.json();
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<p class='muted'>Aún no tienes clips. Ve a Crear Clip.</p>"; return; }
    list.forEach((c) => box.appendChild(clipRow(c, false)));
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar tus clips.</p>"; }
}

async function loadAdmin() {
  loadAdminRequests();
  loadAdminPlans();
  loadAdminMethods();
  try {
    const ru = await fetch("/api/admin/users", { headers: authHeaders() });
    const users = await ru.json();
    $("adminUsers").innerHTML = "<table><tr><th>Nombre</th><th>Correo</th><th>Plan</th><th>Admin</th></tr>" +
      users.map((u) => `<tr><td>${u.name}</td><td>${u.email}</td><td>${u.plan}</td><td>${u.is_admin ? "SÍ" : "No"}</td></tr>`).join("") + "</table>";
  } catch { $("adminUsers").innerHTML = "<p class='muted'>No se pudieron cargar los usuarios.</p>"; }
  try {
    const rc = await fetch("/api/admin/clips", { headers: authHeaders() });
    const clips = await rc.json();
    const box = $("adminClips");
    box.innerHTML = "";
    if (!clips.length) { box.innerHTML = "<p class='muted'>No hay clips todavía.</p>"; return; }
    clips.forEach((c) => {
      const row = clipRow(c, true);
      if (c.status === "pendiente") {
        const acts = document.createElement("div");
        acts.innerHTML = `<button class="small" data-act="approve">Aprobar</button> <button class="small black" data-act="reject">Rechazar</button>`;
        acts.querySelector('[data-act="approve"]').onclick = async () => { await fetch(`/api/admin/clips/${c.id}/approve`, { method: "POST", headers: authHeaders() }); loadAdmin(); loadPanel(); };
        acts.querySelector('[data-act="reject"]').onclick = async () => { await fetch(`/api/admin/clips/${c.id}/reject`, { method: "POST", headers: authHeaders() }); loadAdmin(); loadPanel(); };
        row.appendChild(acts);
      }
      box.appendChild(row);
    });
  } catch { $("adminClips").innerHTML = "<p class='muted'>No se pudieron cargar los clips.</p>"; }
}

// ---------- Panel: Mi Cuenta ----------
$("btnSaveAccount").onclick = async () => {
  try {
    const r = await fetch("/api/auth/me", { method: "PATCH", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: $("accName").value.trim(), brand_text: $("accBrand").value.trim(), frame_enabled: $("accFrame").checked }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Error");
    $("accMsg").textContent = "Guardado.";
    showUser(d);
  } catch (e) { $("accMsg").textContent = "Error: " + e.message; }
};

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
    const r = await fetch("/api/clips", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
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
    const r = await fetch("/api/render", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
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
