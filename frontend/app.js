// ClipViral — frontend (same origin, talks to /api/*)
const $ = (id) => document.getElementById(id);
const state = { clipsJob: null, transcript: null, selected: null, words: null,
  user: null, uploadedFile: null, busy: false, pvClip: null,
  editingClipId: null, coverAt: 0 };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (m) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[m]));
const FRIENDLY_ERROR = "El servidor está despertando o se está actualizando, espera un minuto e inténtalo de nuevo";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- fetch seguro: nunca rompe con JSON vacío o no-JSON ----------
// Todas las llamadas a la API pasan por aquí. Si el servidor responde
// vacío, con HTML o con un 5xx (p. ej. Render reiniciando), el usuario
// ve un mensaje amable en la pantalla, nunca "Failed to execute 'json'".
async function safeJson(r) {
  const text = await r.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}
async function apiFetch(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    const e = new Error(FRIENDLY_ERROR);
    e.status = 0;
    throw e;
  }
  const d = await safeJson(r);
  if (!r.ok) {
    const e = new Error((d && d.detail) || FRIENDLY_ERROR);
    e.status = r.status;
    throw e;
  }
  return d;
}
function authHeaders(extra = {}) {
  const t = localStorage.getItem("clipper_token");
  return t ? { ...extra, Authorization: `Bearer ${t}` } : extra;
}

// ---------- Mi cuenta: registro / login (JWT en localStorage) ----------
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
  document.body.classList.remove("create-wide");
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
  // Banner de impersonación (el admin entró a la cuenta de un usuario).
  const imp = localStorage.getItem("clipper_admin_token");
  $("impBanner").hidden = !imp;
  if (imp) $("impName").textContent = localStorage.getItem("clipper_impersonating") || u.name || "";
}
function doLogout() {
  localStorage.removeItem("clipper_token");
  localStorage.removeItem("clipper_admin_token");
  localStorage.removeItem("clipper_impersonating");
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
    $("panelWelcome").textContent = `Hola, ${user.name} — tus clips y tu cuenta, todo en un solo lugar.`;
    $("tabAdmin").hidden = !user.is_admin;
    // La configuración guardada del usuario manda en el creador.
    if (user.brand_text) $("brandText").value = user.brand_text;
    $("frameEnabled").checked = user.frame_enabled !== false;
    $("frameEnabledVideo").checked = user.frame_enabled !== false;
    fillConfig(user);
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
  if (!t) {
    document.documentElement.classList.remove("booting");
    return showUser(null);
  }
  try {
    const me = await apiFetch("/api/auth/me", { headers: authHeaders() });
    document.documentElement.classList.remove("booting");
    showUser(me);
  } catch (e) {
    // 401 = token inválido: se borra y se muestra el login. Cualquier
    // otro fallo (servidor despertando) conserva el token.
    if (e.status === 401) localStorage.removeItem("clipper_token");
    document.documentElement.classList.remove("booting");
    if (e.status !== 401) $("authMsg").textContent = FRIENDLY_ERROR;
    showUser(null);
  }
}
$("btnLogin").onclick = async () => {
  try {
    const d = await apiFetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("authEmail").value.trim(), password: $("authPass").value }) });
    localStorage.setItem("clipper_token", d.access_token);
    $("authMsg").textContent = "";
    showUser(d.user);
  } catch (e) { $("authMsg").textContent = "Error: " + e.message; }
};
$("btnRegister").onclick = async () => {
  try {
    const d = await apiFetch("/api/auth/register", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("authName").value.trim(), email: $("authEmail").value.trim(), password: $("authPass").value }) });
    localStorage.setItem("clipper_token", d.access_token);
    $("authMsg").textContent = "";
    showUser(d.user);
  } catch (e) { $("authMsg").textContent = "Error: " + e.message; }
};
$("btnLogout").onclick = doLogout;
$("btnLogoutTop").onclick = doLogout;
$("btnShowPanel").onclick = () => openPanel();
$("btnBackAdmin").onclick = () => {
  const adminToken = localStorage.getItem("clipper_admin_token");
  if (adminToken) localStorage.setItem("clipper_token", adminToken);
  localStorage.removeItem("clipper_admin_token");
  localStorage.removeItem("clipper_impersonating");
  location.reload();
};
refreshMe();

// ---------- Panel: pestañas ----------
// Al entrar en Crear Clip el menú lateral se cierra (body.create-wide) para
// que la sección se vea más abierta; cualquier otra pestaña, el logo o el
// botón "← Volver al panel" lo devuelven.
document.querySelectorAll(".panel .tab[data-tab]").forEach((btn) => {
  btn.onclick = () => {
    if (btn.dataset.tab === "creator") { closePanel(); return; }
    document.body.classList.toggle("create-wide", btn.dataset.tab === "create");
    document.querySelectorAll(".panel .tab[data-tab]").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".panel .pane").forEach((p) => p.hidden = true);
    $("pane-" + btn.dataset.tab).hidden = false;
    if (btn.dataset.tab === "clips") loadMyClips();
    if (btn.dataset.tab === "admin") loadAdmin();
  };
});
$("btnBackToPanel").onclick = () => document.querySelector('[data-tab="home"]').click();
$("brandHome").onclick = () => document.querySelector('[data-tab="home"]').click();

// ---------- Panel Admin: pestañas (una sección a la vez) ----------
document.querySelectorAll("#adminTabs .mini-tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#adminTabs .mini-tab").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".admin-pane").forEach((p) => p.hidden = true);
    $("adminPane-" + btn.dataset.admin).hidden = false;
    const loaders = { users: loadAdminUsers, clips: loadAdminClips, requests: loadAdminRequests,
      plans: loadAdminPlans, methods: loadAdminMethods,
      settings: loadAdminSettings };
    if (loaders[btn.dataset.admin]) loaders[btn.dataset.admin]();
  };
});

// ---------- Configuración del creador: pestañas Plantilla / Clips / Video ----------
document.querySelectorAll(".panel [data-cfg]").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".panel [data-cfg]").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".cfg-pane").forEach((p) => p.hidden = true);
    $("cfg-" + btn.dataset.cfg).hidden = false;
  };
});

function fillConfig(u) {
  $("cfgTemplate").value = u.template || "Gancho grande";
  $("cfgMaxClips").value = u.max_clips ?? 5;
  $("cfgMinClip").value = u.min_clip_secs ?? 20;
  $("cfgMaxClip").value = u.max_clip_secs ?? 45;
  $("cfgQuality").value = u.quality || "1080p";
}

async function loadPanel() {
  // ¿Tiene plan activo? Si no, se bloquean Inicio / Mis Clips / Crear / Cuenta.
  // El ADMIN nunca se bloquea: no necesita plan para usar la plataforma.
  let hasPlan = !!(state.user && state.user.is_admin), activePlanId = null;
  try {
    const d = await apiFetch("/api/subscriptions/mine", { headers: authHeaders() });
    hasPlan = hasPlan || !!d.active_subscription;
    activePlanId = d.active_subscription ? d.active_subscription.plan_id : null;
    renderMyRequests(d.requests || []);
  } catch { /* sin plan */ }
  ["home", "clips", "create", "account"].forEach((t) => {
    const pane = $("pane-" + t);
    if (pane) pane.classList.toggle("is-locked", !hasPlan);
  });
  try {
    const s = await apiFetch("/api/me/stats", { headers: authHeaders() });
    $("stMonth").textContent = s.clips_this_month ?? 0;
    $("stProcessing").textContent = s.videos_in_process ?? 0;
    $("stTotalClips").textContent = s.clips_total ?? 0;
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
    _plans = await apiFetch("/api/plans");
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
    _methods = await apiFetch("/api/payment-methods", { headers: authHeaders() });
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
    await apiFetch("/api/subscriptions", { method: "POST", headers: authHeaders(), body: fd });
    $("buyMsg").textContent = "Solicitud enviada. Queda Pendiente hasta que el admin la apruebe.";
    $("buyBox").hidden = true;
    loadPanel();
  } catch (e) { $("buyMsg").textContent = "Error: " + e.message; }
};

// ---------- Panel Admin ----------
async function loadAdminUsers() {
  try {
    const [users, plans] = await Promise.all([
      apiFetch("/api/admin/users", { headers: authHeaders() }),
      apiFetch("/api/plans"),
    ]);
    const planOpts = plans.map((p) => `<option value="${p.id}">${p.name} · RD$ ${p.price_dop.toLocaleString("es-DO")} · ${p.duration_days} días</option>`).join("");
    $("adminUsers").innerHTML = "<table><tr><th>Nombre</th><th>Correo</th><th>Plan</th><th>Admin</th><th></th></tr>" +
      users.map((u) => {
        const exp = u.plan_expires ? ` <small>· hasta ${u.plan_expires.slice(0, 10)}</small>` : "";
        const assign = u.is_admin ? "" : ` <select data-plan-sel="${u.id}">${planOpts}</select> <button class="small" data-assign="${u.id}">Asignar plan</button>`;
        return `<tr><td>${u.name}</td><td>${u.email}</td><td>${u.plan}${exp}</td><td>${u.is_admin ? "SÍ" : "No"}</td>
        <td>${u.is_admin ? "" : `<button class="small" data-imp="${u.id}" data-name="${u.name}">Entrar como usuario</button>`}${assign}</td></tr>`;
      }).join("") + "</table><p id=\"usersMsg\" class=\"muted\"></p>";
    $("adminUsers").querySelectorAll("[data-imp]").forEach((b) => b.onclick = () => impersonateUser(b.dataset.imp, b.dataset.name));
    $("adminUsers").querySelectorAll("[data-assign]").forEach((b) => b.onclick = async () => {
      const sel = $("adminUsers").querySelector(`[data-plan-sel="${b.dataset.assign}"]`);
      const planId = parseInt(sel.value, 10);
      const msg = $("usersMsg");
      try {
        const r = await apiFetch(`/api/admin/users/${b.dataset.assign}/assign-plan`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ plan_id: planId }) });
        await loadAdminUsers();
        const m2 = $("usersMsg");
        if (m2) m2.textContent = `Plan ${r.subscription.plan_name} asignado hasta ${r.subscription.ends_at.slice(0, 10)}.`;
      } catch (e) { if (msg) msg.textContent = "Error: " + e.message; }
    });
  } catch { $("adminUsers").innerHTML = "<p class='muted'>No se pudieron cargar los usuarios.</p>"; }
}
async function impersonateUser(userId, name) {
  try {
    const d = await apiFetch(`/api/admin/impersonate/${userId}`, { method: "POST", headers: authHeaders() });
    localStorage.setItem("clipper_admin_token", localStorage.getItem("clipper_token"));
    localStorage.setItem("clipper_impersonating", name);
    localStorage.setItem("clipper_token", d.access_token);
    location.reload();
  } catch (e) { alert("Error: " + e.message); }
}
async function loadAdminRequests() {
  try {
    const list = await apiFetch("/api/admin/subscriptions", { headers: authHeaders() });
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
        acts.querySelector('[data-act="approve"]').onclick = async () => { await apiFetch(`/api/admin/subscriptions/${s.id}/approve`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({}) }); loadAdminRequests(); loadPanel(); };
        acts.querySelector('[data-act="reject"]').onclick = async () => { await apiFetch(`/api/admin/subscriptions/${s.id}/reject`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({}) }); loadAdminRequests(); loadPanel(); };
        div.appendChild(acts);
      }
      box.appendChild(div);
    });
  } catch { $("adminRequests").innerHTML = "<p class='muted'>No se pudieron cargar las solicitudes.</p>"; }
}
async function loadAdminPlans() {
  try {
    const plans = await apiFetch("/api/plans");
    $("adminPlans").innerHTML = plans.map((p) =>
      `<div class="rowline"><div><b>${p.name}</b> · RD$ ${p.price_dop.toLocaleString("es-DO")} · ${p.duration_days} días ${p.is_popular ? "· ⭐ Popular" : ""}</div>
      <button class="small black" data-del="${p.id}">Eliminar</button></div>`).join("") || "<p class='muted'>No hay planes.</p>";
    $("adminPlans").querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => { await apiFetch(`/api/admin/plans/${b.dataset.del}`, { method: "DELETE", headers: authHeaders() }); loadAdminPlans(); renderPlans(); });
  } catch { $("adminPlans").innerHTML = "<p class='muted'>No se pudieron cargar los planes.</p>"; }
}
async function loadAdminMethods() {
  try {
    const methods = await apiFetch("/api/payment-methods", { headers: authHeaders() });
    const box = $("adminMethods");
    box.innerHTML = "";
    methods.forEach((m) => {
      const div = document.createElement("div");
      div.className = "rowline";
      div.innerHTML = `<div><b>${m.name}</b> · ${m.bank} · ${m.account_number}<br><small>${m.account_holder} · ${m.account_type}</small></div>
        <button class="small black" data-del="${m.id}">Eliminar</button>`;
      div.querySelector("[data-del]").onclick = async () => { await apiFetch(`/api/admin/payment-methods/${m.id}`, { method: "DELETE", headers: authHeaders() }); loadAdminMethods(); };
      box.appendChild(div);
    });
    if (!methods.length) box.innerHTML = "<p class='muted'>No hay métodos activos. Crea uno abajo.</p>";
  } catch { $("adminMethods").innerHTML = "<p class='muted'>No se pudieron cargar los métodos.</p>"; }
}
$("btnNewPlan").onclick = async () => {
  const body = { name: $("npName").value.trim(), price_dop: parseInt($("npPrice").value || "0", 10), duration_days: parseInt($("npDays").value || "30", 10),
    features: $("npFeatures").value.split("\n").map((s) => s.trim()).filter(Boolean), is_popular: $("npPopular").checked, is_active: true, sort_order: 99 };
  if (!body.name) return alert("Ponle un nombre al plan");
  try {
    await apiFetch("/api/admin/plans", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    $("npName").value = ""; $("npFeatures").value = "";
    loadAdminPlans(); renderPlans();
  } catch { alert("No se pudo crear el plan"); }
};
$("btnNewMethod").onclick = async () => {
  const body = { name: $("nmName").value.trim(), bank: $("nmBank").value.trim(), account_number: $("nmAccount").value.trim(),
    account_holder: $("nmHolder").value.trim(), account_type: $("nmType").value.trim(), instructions: $("nmInstr").value.trim(), is_active: true };
  if (!body.name) return alert("Ponle un nombre al método");
  try {
    await apiFetch("/api/admin/payment-methods", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    ["nmName", "nmBank", "nmAccount", "nmHolder", "nmType", "nmInstr"].forEach((id) => $(id).value = "");
    loadAdminMethods();
  } catch { alert("No se pudo crear el método"); }
};
async function loadAdminSettings() {
  try {
    const s = await apiFetch("/api/admin/settings", { headers: authHeaders() });
    $("setBrand").value = s.default_brand_text || "@marketingcaribe";
    $("setFrame").checked = String(s.default_frame_enabled).toLowerCase() === "true";
  } catch { $("setMsg").textContent = "No se pudo cargar la configuración."; }
}
$("btnSaveSettings").onclick = async () => {
  try {
    await apiFetch("/api/admin/settings", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ settings: { default_brand_text: $("setBrand").value.trim() || "@marketingcaribe", default_frame_enabled: $("setFrame").checked ? "true" : "false" } }) });
    $("setMsg").textContent = "Configuración guardada. Los usuarios nuevos recibirán estos valores.";
  } catch (e) { $("setMsg").textContent = "Error: " + e.message; }
};

// Mis Clips: tarjetas verticales 9:16 estilo Biblioteca — video real,
// badge verde de viralidad + duración sobre la miniatura, título,
// Descargar y Editar. Los clips ya NO necesitan aprobación del admin:
// se descargan apenas se renderizan. El badge de viralidad solo sale
// cuando el clip tiene score guardado; los viejos (sin score) no
// muestran un número inventado.
function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function clipRow(c, admin) {
  const div = document.createElement("div");
  div.className = "clip-card";
  const dur = fmtDur((c.end || 0) - (c.start || 0));
  const badge = (typeof c.score === "number")
    ? `<span class="viral-badge">${Math.round(c.score)}% viralidad</span>` : "";
  const thumb = c.download_url
    ? `<video muted playsinline preload="metadata" src="${c.download_url}${typeof c.cover_at === "number" ? "#t=" + c.cover_at : ""}"></video>`
    : `<div class="clip-thumb-empty">Sin video</div>`;
  const date = c.created_at ? c.created_at.slice(0, 10) : "";
  const meta = [c.uploader || "", date].filter(Boolean).join(" · ");
  const who = admin
    ? `<div class="muted small">${esc(c.user_name || "")} · ${esc(c.user_email || "")}</div>` : "";
  div.innerHTML = `<div class="clip-thumb">${thumb}
      <div class="clip-badges">${badge}<span class="dur-badge">${dur}</span></div>
    </div>
    <div class="clip-body">
      <div class="clip-title">${esc(c.title || "Clip")}</div>
      ${meta ? `<div class="muted small">${esc(meta)}</div>` : ""}
      ${who}
      <div class="clip-actions">
        ${c.download_url ? `<a href="${c.download_url}" download><button class="small">Descargar</button></a>` : ""}
        <button class="small ghost">Editar</button>
      </div>
    </div>`;
  div.querySelector(".ghost").onclick = () => editClip(c);
  return div;
}

// Editar: reabre el clip en el editor de subtítulos del creador
// (paso 3), con su transcripción y su rango, para corregir subtítulos
// y volver a renderizar. Re-renderizar crea un clip nuevo en Mis Clips
// (el original queda intacto).
async function editClip(c) {
  if (!c.job_id) return alert("Este clip no tiene su trabajo original para editar.");
  try {
    const d = await apiFetch(`/api/jobs/${c.job_id}/transcript`, { headers: authHeaders() });
    const sec = (typeof c.section_start === "number") ? c.section_start
      : (typeof d.section_start === "number" ? d.section_start : null);
    if (sec === null || !d.transcript || !Array.isArray(d.transcript.segments)) {
      return alert("No se pudo cargar la transcripción de este clip.");
    }
    state.clipsJob = { id: c.job_id, result: { section_start: sec } };
    state.transcript = d.transcript;
    state.editingClipId = c.id;
    const words = clipWords(c.start, c.end);
    const cand = {
      start: +(c.start - sec).toFixed(2), end: +(c.end - sec).toFixed(2),
      abs_start: c.start, abs_end: c.end,
      text: words.map((w) => w.w.trim()).join(" ").trim() || c.title || "",
      title: c.title || "",
      score: (typeof c.score === "number" ? c.score : null),
      cover_at: (typeof c.cover_at === "number" ? c.cover_at : null),
    };
    document.querySelector('[data-tab="create"]').click();
    selectClip(cand, null);
  } catch (e) {
    alert("No se pudo abrir el editor: " + (e.message || "error"));
  }
}

async function loadMyClips() {
  const box = $("myClips");
  try {
    const list = await apiFetch("/api/me/clips", { headers: authHeaders() });
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<p class='muted'>Aún no tienes clips. Ve a Crear Clip.</p>"; return; }
    list.forEach((c) => box.appendChild(clipRow(c, false)));
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar tus clips.</p>"; }
}
async function loadAdminClips() {
  try {
    const clips = await apiFetch("/api/admin/clips", { headers: authHeaders() });
    const box = $("adminClips");
    box.innerHTML = "";
    if (!clips.length) { box.innerHTML = "<p class='muted'>No hay clips todavía.</p>"; return; }
    clips.forEach((c) => box.appendChild(clipRow(c, true)));
  } catch { $("adminClips").innerHTML = "<p class='muted'>No se pudieron cargar los clips.</p>"; }
}
function loadAdmin() {
  loadAdminUsers();
  loadAdminClips();
  loadAdminRequests();
  loadAdminPlans();
  loadAdminMethods();
  loadAdminSettings();
}

// ---------- Panel: Mi Cuenta ----------
$("btnSaveAccount").onclick = async () => {
  try {
    const d = await apiFetch("/api/auth/me", { method: "PATCH", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: $("accName").value.trim(), brand_text: $("accBrand").value.trim(), frame_enabled: $("accFrame").checked }) });
    $("accMsg").textContent = "Guardado.";
    showUser(d);
  } catch (e) { $("accMsg").textContent = "Error: " + e.message; }
};

// ---------- Poll de jobs (con reintentos ante respuestas vacías) ----------
async function pollJob(jobId, onProgress) {
  let fails = 0;
  while (true) {
    let j;
    try {
      j = await apiFetch(`/api/jobs/${jobId}`);
      fails = 0;
    } catch (e) {
      // El servidor puede estar reiniciando: reintentar unas veces antes
      // de rendirse con un mensaje amable.
      if (++fails >= 5) throw new Error(FRIENDLY_ERROR);
      await sleep(3000);
      continue;
    }
    if (onProgress) onProgress(j);
    if (j.status === "done") return j;
    if (j.status === "error") {
      const err = new Error("JOB_FAILED");
      err.raw = j.error || "";
      throw err;
    }
    await sleep(2000);
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
$("frameEnabledVideo").addEventListener("change", () => {
  $("frameEnabled").checked = $("frameEnabledVideo").checked;
  updatePreview();
});
$("frameEnabled").addEventListener("change", () => {
  $("frameEnabledVideo").checked = $("frameEnabled").checked;
});
updatePreview();

// ---------- Crear Clip: 5 pasos (Descargar → Transcribir → IA elige → Cortar → Listo) ----------
const FLOW_MSGS = [
  "Descargando el video…",
  "Transcribiendo el audio…",
  "La IA está eligiendo los mejores momentos…",
  "Cortando tu clip…",
  "Listo. Tu clip está pronto para descargar.",
];
// Iconos de cada paso (los puntos muestran el icono, no un número).
// El paso terminado muestra un check; el activo, su propio icono con brillo.
const _svgOpen = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
const CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 12.5l5 5L19.5 7"/></svg>';
const STEP_ICONS = [
  _svgOpen + '<path d="M12 3v12"/><path d="M6 11l6 6 6-6"/><path d="M4 21h16"/></svg>', // Descargar
  _svgOpen + '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>', // Transcribir
  _svgOpen + '<path d="M12 3l1.8 4.7L18.5 9l-4.7 1.8L12 15.5l-1.8-4.7L5.5 9l4.7-1.3z"/><path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8z"/></svg>', // IA elige
  _svgOpen + '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.12 15.88"/><path d="M14.47 14.48 20 20"/><path d="M8.12 8.12 12 12"/></svg>', // Cortar
  CHECK_SVG, // Listo
];
function renderSteps(doneCount, activeIdx, statusText) {
  document.querySelectorAll("#stepsBar .step").forEach((el, i) => {
    el.classList.toggle("done", i < doneCount);
    el.classList.toggle("active", i === activeIdx);
    // innerHTML, nunca textContent: el punto lleva un SVG y un texto lo borraría.
    el.querySelector(".dot").innerHTML = i < doneCount ? CHECK_SVG : STEP_ICONS[i];
  });
  if (statusText !== undefined) {
    const st = $("stepStatus");
    // Mientras el trabajo está en marcha el texto lleva un punto rojo que
    // palpita y puntos suspensivos animados; al cambiar, entra con fade.
    // Solo se re-anima si el texto o el estado cambian (el poll llama
    // cada 2 s con el mismo texto y no debe parpadear).
    const busy = activeIdx >= 0 && statusText !== "";
    if (st.dataset.txt !== statusText || st.dataset.busy !== String(busy)) {
      st.dataset.txt = statusText;
      st.dataset.busy = String(busy);
      st.classList.toggle("busy", busy);
      if (busy) {
        const clean = statusText.replace(/…+$/, "");
        st.innerHTML = clean + '<span class="ellipsis" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span>';
      } else {
        st.textContent = statusText;
      }
      st.classList.remove("swap");
      void st.offsetWidth;
      st.classList.add("swap");
    }
  }
}
function setJobProgress(p) {
  const f = $("progressFill");
  if (f) f.style.width = Math.max(0, Math.min(100, p || 0)) + "%";
}
// ---------- Vista previa (en vivo): solo cuando hay un clip que ver ----------
// Oculta por defecto (hidden en el HTML). Aparece al elegir un clip o al
// terminar un render; se esconde de nuevo al empezar otro trabajo o al
// cambiar el link / subir otro archivo.
function showPreview() { $("previewCard").hidden = false; }
function hidePreview() {
  $("previewCard").hidden = true;
  const pv = $("pvVideo");
  if (pv) pv.pause();
}
function clipsStepFromProgress(p) {
  if (p < 30) return 0;
  if (p < 70) return 1;
  return 2;
}
function showJobError(friendly) {
  const box = $("jobError");
  box.style.display = "block";
  box.innerHTML = `<b>No se pudo completar.</b> ${friendly}`;
}
function hideJobError() {
  $("jobError").style.display = "none";
  $("jobError").innerHTML = "";
}
function resetCreateFlow() {
  state.clipsJob = null;
  state.transcript = null;
  state.selected = null;
  state.words = null;
  state.pvClip = null;
  $("candidatesCard").hidden = true;
  $("step3Card").hidden = true;
  $("resultCard").hidden = true;
  hidePreview();
  setJobProgress(0);
  $("candidates").innerHTML = "<p class='muted'>Primero genera los clips.</p>";
  $("stepsCard").hidden = false;
  hideJobError();
  renderSteps(0, 0, "Esperando un video. Pega un link arriba para empezar…");
}

// ---------- Subir archivo ----------
// Cambiar el link o subir otro archivo esconde la vista previa: ya no hay
// un clip que mostrar hasta elegir uno nuevo.
$("url").addEventListener("input", hidePreview);
$("btnUpload").onclick = () => $("uploadFile").click();
$("uploadFile").onchange = async () => {
  const file = $("uploadFile").files[0];
  if (!file) return;
  hidePreview();
  const mb = (file.size / (1024 * 1024)).toFixed(1);
  const info = $("uploadInfo");
  info.hidden = false;
  info.textContent = `Subiendo ${file.name} (${mb} MB)…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const d = await apiFetch("/api/upload", { method: "POST", headers: authHeaders(), body: fd });
    state.uploadedFile = d.file_id;
    info.textContent = `Archivo listo: ${d.filename} (${mb} MB). Dale a "Sacar clips".`;
    $("videoMeta").textContent = "";
  } catch (e) {
    state.uploadedFile = null;
    info.textContent = "Error: " + e.message;
  }
};

// ---------- Sacar clips (un solo botón: analiza + genera) ----------
$("btnClips").onclick = async () => {
  if (state.busy) return;
  const url = $("url").value.trim();
  if (!state.uploadedFile && !url) return alert("Pega un link de YouTube o sube un video");
  state.busy = true;
  $("btnClips").disabled = true;
  $("btnUpload").disabled = true;
  state.editingClipId = null;
  resetCreateFlow();
  state.videoTitle = "";
  try {
    let body;
    if (state.uploadedFile) {
      body = { source_file: state.uploadedFile };
    } else {
      // Validar el link y ajustar el rango con la duración real.
      renderSteps(0, 0, "Leyendo el video…");
      const info = await apiFetch("/api/video-info", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ url }) });
      const mins = info.duration ? `${Math.floor(info.duration / 60)}:${String(info.duration % 60).padStart(2, "0")}` : "live";
      $("videoMeta").textContent = `${info.title} · ${info.uploader || ""} · ${mins}`;
      state.videoTitle = info.title || "";
      // No pisar el "Hasta" que el usuario ya escribió: solo poner el
      // valor por defecto si está vacío, y recortarlo a la duración real.
      const userEnd = parseFloat($("rangeEnd").value);
      if (!userEnd || userEnd <= 0) {
        $("rangeEnd").value = Math.min(info.duration || 120, 300);
      } else if (info.duration) {
        $("rangeEnd").value = Math.min(userEnd, info.duration);
      }
      body = { url };
    }
    body.start = parseFloat($("rangeStart").value || 0);
    body.end = parseFloat($("rangeEnd").value || 120);
    body.model = $("model").value;
    body.max_clips = parseInt($("cfgMaxClips").value || "5", 10);
    body.min_len = parseFloat($("cfgMinClip").value || "20");
    body.max_len = parseFloat($("cfgMaxClip").value || "45");
    const d = await apiFetch("/api/clips", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    const job = await pollJob(d.job_id, (j) => {
      setJobProgress(j.progress || 0);
      renderSteps(clipsStepFromProgress(j.progress || 0), clipsStepFromProgress(j.progress || 0), FLOW_MSGS[clipsStepFromProgress(j.progress || 0)]);
    });
    setJobProgress(100);
    state.clipsJob = job;
    state.transcript = job.result.transcript;
    renderSteps(3, -1, `La IA encontró ${job.result.candidates.length} clip${job.result.candidates.length === 1 ? "" : "s"}, elige uno para continuar.`);
    const box = $("candidates");
    box.innerHTML = "";
    if (!job.result.candidates.length) {
      $("candidatesCard").hidden = false;
      box.innerHTML = "<p class='muted'>No se encontraron clips en ese rango. Prueba otro rango.</p>";
      return;
    }
    $("candidatesTitle").textContent = `La IA encontró ${job.result.candidates.length} clips, elige uno para continuar`;
    job.result.candidates.forEach((c, i) => {
      const div = document.createElement("div");
      div.className = "clip";
      // Miniatura real: el video de la sección, en el primer fotograma de
      // este clip (#t= con el inicio relativo a la sección).
      div.innerHTML = `<video muted playsinline preload="metadata" src="/api/jobs/${job.id}/source#t=${c.start.toFixed(1)}"></video>`
        + `<div class="clip-body"><div class="clip-meta">Clip ${i + 1} · ${c.abs_start}s – ${c.abs_end}s (${c.duration}s) <span class="clip-score">⭐ ${c.score}</span></div>`
        + `<small class="clip-text">${esc(c.text.slice(0, 160))}…</small></div>`;
      div.onclick = () => { state.editingClipId = null; selectClip(c, div); };
      box.appendChild(div);
    });
    $("candidatesCard").hidden = false;
    $("candidatesCard").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    console.error("Sacar clips falló:", e.raw || e.message);
    renderSteps(0, -1, "");
    showJobError("No se pudo procesar el video. Revisa el enlace o el archivo e inténtalo de nuevo.");
  } finally {
    state.busy = false;
    $("btnClips").disabled = false;
    $("btnUpload").disabled = false;
  }
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

// ---------- Vista previa en vivo con video real ----------
// Agrupa las palabras como el render (3 a la vez, corte en pausas > 0.8 s).
function wordGroups(words) {
  const groups = [];
  let cur = [];
  for (const w of words || []) {
    if (!String(w.w).trim()) continue;
    if (cur.length && w.s - cur[cur.length - 1].e > 0.8) { groups.push(cur); cur = []; }
    cur.push(w);
    if (cur.length >= 3) { groups.push(cur); cur = []; }
  }
  if (cur.length) groups.push(cur);
  return groups;
}

// Subtítulos karaoke del momento actual de la vista previa: 3 palabras,
// la que suena va en amarillo (igual que el clip exportado).
function updatePreviewSubs(t) {
  const el = $("pvSubs");
  if (!state.words || !state.words.length) return;
  const groups = wordGroups(state.words);
  let g = groups.find((gr) => t >= gr[0].s && t <= gr[gr.length - 1].e + 0.15);
  if (!g) g = groups[0];
  if (!g) return;
  el.innerHTML = g.map((w) => {
    const txt = w.w.trim().toUpperCase();
    return (t >= w.s && t < w.e) ? `<span>${esc(txt)}</span>` : esc(txt);
  }).join(" ");
}

// Reproduce el trozo del clip elegido en la vista previa 9:16 (sin sonido).
function playPreviewClip(c) {
  const pv = $("pvVideo");
  if (!pv || !state.clipsJob) return;
  const src = `/api/jobs/${state.clipsJob.id}/source`;
  state.pvClip = { start: c.start, end: c.end };
  const startAt = () => {
    // Si el usuario ya eligió otro clip mientras cargaban los metadatos,
    // este arranque queda obsoleto.
    if (!state.pvClip || state.pvClip.start !== c.start) return;
    try { pv.currentTime = Math.max(0, c.start + 0.01); } catch (e) { /* aún sin metadatos */ }
    pv.play().catch(() => {});
  };
  if (pv.dataset.src !== src) {
    pv.dataset.src = src;
    pv.src = src;
    pv.addEventListener("loadedmetadata", startAt, { once: true });
    pv.load();
  } else {
    startAt();
  }
}
$("pvVideo").addEventListener("timeupdate", () => {
  const pv = $("pvVideo");
  if (!state.pvClip) return;
  const dur = state.pvClip.end - state.pvClip.start;
  const t = pv.currentTime - state.pvClip.start;
  if (t >= dur) { // fin del clip: vuelve al inicio (bucle del trozo)
    try { pv.currentTime = state.pvClip.start + 0.01; } catch (e) {}
    return;
  }
  if (t >= 0) updatePreviewSubs(t);
});

function selectClip(c, el) {
  document.querySelectorAll(".clip").forEach((x) => x.classList.remove("sel"));
  if (el) el.classList.add("sel");
  state.selected = c;
  state.words = clipWords(c.abs_start, c.abs_end);
  $("clipStart").value = c.abs_start;
  $("clipEnd").value = c.abs_end;
  $("subText").value = c.text;
  // Título de enganche + portada del paso 3: el título del candidato
  // (LLM) o el guardado del clip (Editar); la portada guardada o 0.
  $("hookTitle").value = c.title || "";
  const clipDur = Math.max(0, (c.abs_end || 0) - (c.abs_start || 0));
  const cov = $("coverRange");
  cov.max = Math.max(clipDur, 0.1);
  const startCover = (typeof c.cover_at === "number")
    ? Math.min(Math.max(c.cover_at, 0), clipDur) : 0;
  cov.value = startCover;
  state.coverAt = startCover;
  $("coverVal").textContent = startCover.toFixed(1) + " s";
  $("btnSaveMeta").hidden = !state.editingClipId;
  $("saveMetaMsg").textContent = "";
  $("pvSubs").innerHTML = state.words.slice(0, 3).map((w, i) => i === 1 ? `<span>${w.w.trim().toUpperCase()}</span>` : w.w.trim().toUpperCase()).join(" ") || "SUBTÍTULOS";
  // Ya hay un clip que ver: aparece la vista previa (encima de Configuración)
  // y empieza a reproducir el trozo elegido, con marco, marca y subtítulos.
  showPreview();
  playPreviewClip(c);
  updatePreview();
  // El paso 3 (subtítulos + render) solo aparece DESPUÉS de elegir un clip.
  $("step3Card").hidden = false;
  $("resultCard").hidden = true;
  $("step3Card").scrollIntoView({ behavior: "smooth" });
}

// ---------- Guardar configuración del creador ----------
$("btnSaveConfig").onclick = async () => {
  const body = {
    template: $("cfgTemplate").value,
    brand_text: $("brandText").value.trim(),
    frame_enabled: $("frameEnabled").checked,
    max_clips: parseInt($("cfgMaxClips").value || "5", 10),
    min_clip_secs: parseFloat($("cfgMinClip").value || "20"),
    max_clip_secs: parseFloat($("cfgMaxClip").value || "45"),
    quality: $("cfgQuality").value,
    subtitles_enabled: $("showSubs").value === "1",
  };
  try {
    const d = await apiFetch("/api/auth/me", { method: "PATCH", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    state.user = d;
    updateTopBar();
    $("cfgMsg").textContent = "Configuración guardada.";
  } catch (e) { $("cfgMsg").textContent = "Error: " + e.message; }
};

// ---------- Portada: el control mueve la vista previa al fotograma ----------
// Pausa el video en el momento elegido (dentro del clip) para que el
// usuario vea el fotograma que quedará como miniatura en Mis Clips.
$("coverRange").addEventListener("input", () => {
  const v = parseFloat($("coverRange").value || "0");
  state.coverAt = v;
  $("coverVal").textContent = v.toFixed(1) + " s";
  const pv = $("pvVideo");
  if (pv && state.selected) {
    pv.pause();
    try { pv.currentTime = Math.max(0, state.selected.start + v + 0.01); } catch (e) { /* aún sin metadatos */ }
  }
});

// ---------- Guardar título y portada (sin re-renderizar) ----------
// Solo visible cuando se está editando un clip existente: actualiza el
// título y la portada del clip en Mis Clips, sin crear un render nuevo.
$("btnSaveMeta").onclick = async () => {
  if (!state.editingClipId) return;
  const msg = $("saveMetaMsg");
  msg.textContent = "Guardando…";
  try {
    await apiFetch(`/api/me/clips/${state.editingClipId}`, { method: "PATCH", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ title: $("hookTitle").value.trim(), cover_at: state.coverAt }) });
    msg.textContent = "Guardado ✓";
    loadMyClips();
    if (state.user && state.user.is_admin) loadAdminClips();
  } catch (e) { msg.textContent = "Error: " + e.message; }
};

// ---------- Cámara (paso 3) ----------
const CAMERA_HINTS = {
  center: "Centrada: el recorte 9:16 fijo en el centro del video, como siempre.",
  follow_speaker: "La cámara sigue a la persona que está hablando. Si no se ve ninguna cara, queda centrada.",
  follow_person: "Sigue siempre a la persona de la zona que elijas. Si no se la ve, queda centrada.",
  dual: "Para entrevistas con dos personas sentadas a izquierda y derecha: la mitad izquierda arriba y la derecha abajo.",
};
$("cameraMode").onchange = () => {
  const m = $("cameraMode").value;
  $("cameraPersonRow").hidden = m !== "follow_person";
  $("cameraHint").textContent = CAMERA_HINTS[m] || CAMERA_HINTS.center;
};

// ---------- Renderizar ----------
$("btnRender").onclick = async () => {
  if (!state.clipsJob) return alert("Primero genera los clips y elige uno");
  if (state.busy) return;
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
  state.busy = true;
  $("btnRender").disabled = true;
  hideJobError();
  renderSteps(3, 3, FLOW_MSGS[3]);
  try {
    const body = {
      job_id: state.clipsJob.id, start, end, model: $("model").value,
      quality: $("cfgQuality").value,
      frame_enabled: $("frameEnabled").checked,
      frame_color: $("frameColor").value,
      frame_width: parseInt($("frameWidth").value || "14", 10),
      brand_text: $("brandText").value,
      title_text: $("titleText").value,
      // Identidad del clip en la biblioteca: título gancho + viralidad
      // del candidato elegido (LLM) o su score (regla); el título del
      // video original como respaldo. El título se toma del campo
      // editable del paso 3, y la portada del control de portada.
      clip_title: $("hookTitle").value.trim() || (state.selected && state.selected.title) || "",
      cover_at: state.coverAt,
      camera_mode: $("cameraMode").value,
      camera_person: $("cameraPerson").value,
      clip_score: (state.selected && typeof state.selected.score === "number")
        ? state.selected.score : null,
      video_title: state.videoTitle || "",
      show_subtitles: $("showSubs").value === "1",
      subtitle_style: $("subStyle").value,
      subtitle_size: parseInt($("subSize").value || "76", 10),
      subtitle_position: $("subPos").value,
      edited_words: edited,
    };
    const d = await apiFetch("/api/render", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    const job = await pollJob(d.job_id, (j) => {
      setJobProgress(j.progress || 0);
      if ((j.progress || 0) >= 70) renderSteps(3, 3, FLOW_MSGS[3]);
    });
    const file = job.result.file;
    setJobProgress(100);
    renderSteps(5, -1, FLOW_MSGS[4]);
    $("resultCard").hidden = false;
    showPreview();
    $("result").src = `/api/download/${file}`;
    $("downloadLink").href = `/api/download/${file}`;
    $("downloadLink").download = file;
    document.getElementById("resultCard").scrollIntoView({ behavior: "smooth" });
    loadPanel();
    loadMyClips();
    // El render creó un clip nuevo: ya no se está editando el anterior.
    state.editingClipId = null;
    $("btnSaveMeta").hidden = true;
  } catch (e) {
    console.error("Render falló:", e.raw || e.message);
    showJobError(e.message === "JOB_FAILED" ? "No se pudo cortar el clip. Inténtalo de nuevo." : e.message);
  } finally {
    state.busy = false;
    $("btnRender").disabled = false;
  }
};
