// ClipViral — frontend (same origin, talks to /api/*)
const $ = (id) => document.getElementById(id);
const state = { clipsJob: null, transcript: null, selected: null, words: null,
  user: null, uploadedFile: null, busy: false, pvClip: null,
  editingClipId: null, coverAt: 0, canImages: false, canVideos: false,
  videoModels: null, vidPollTimer: null, imageModels: null, imgPollTimer: null,
  userTemplate: "Gancho grande", templateTouched: false };

// Plantillas (mismo preset que app/render.py: TEMPLATES). El editor
// aplica la plantilla a los controles; si el usuario toca un control de
// la plantilla (marco, color, grosor, posición), el render es
// personalizado y se envía template=null.
const TEMPLATES_JS = {
  "Gancho grande": { frameEnabled: true, frameColor: "#FADE4C", frameWidth: 14, subPos: "bottom", hlColor: "#FADE4C" },
  "Limpio": { frameEnabled: false, frameColor: "#FADE4C", frameWidth: 14, subPos: "bottom", hlColor: "#FADE4C" },
  "Impacto": { frameEnabled: true, frameColor: "#E10600", frameWidth: 18, subPos: "bottom", hlColor: "#E10600" },
};
function applyTemplateToControls(name) {
  const t = TEMPLATES_JS[name] || TEMPLATES_JS["Gancho grande"];
  $("frameEnabled").checked = t.frameEnabled;
  $("frameEnabledVideo").checked = t.frameEnabled;
  $("frameColor").value = t.frameColor;
  $("frameWidth").value = t.frameWidth;
  $("subPos").value = t.subPos;
  $("hlColor").value = t.hlColor;
  state.templateTouched = false;
  updatePreview();
}
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
  // Abrir la sección que dice la URL (enlace directo o recarga).
  openTabFromUrl();
}
function closePanel() {
  $("panel").hidden = true;
  document.body.classList.remove("panel-open");
  document.body.classList.remove("create-wide");
  document.querySelector("main").hidden = false;
  document.querySelector("header").hidden = false;
}
// Cuenta atrás del plan en la barra superior (2026-11-10, Juan: en
// vez de "Hola, nombre", el tiempo que le queda a su plan). El admin
// tiene plan_expires = created_at + 30 días; un usuario sin plan tiene
// plan_expires null y NUNCA se le inventa una fecha.
function planCountdownText(expiresIso) {
  if (!expiresIso) return null;
  const ms = new Date(expiresIso).getTime() - Date.now();
  if (Number.isNaN(ms) || ms <= 0) return "Plan vencido";
  const days = Math.floor(ms / 86400000);
  if (days >= 1) return `⏳ Quedan ${days} día${days === 1 ? "" : "s"}`;
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return `⏳ Quedan ${h} h ${m} min`;
}
function updateTopBar() {
  const u = state.user;
  if (!u) return;
  // Barra superior: cuenta atrás del plan + insignia. El ADMIN siempre
  // es "Admin" (roja), aunque su campo plan sea "free"; un usuario
  // normal sin plan ve "Sin plan", y con plan ve el nombre de su plan.
  const cd = planCountdownText(u.plan_expires);
  const el = $("panelCountdown");
  if (cd) {
    el.textContent = cd;
    el.classList.toggle("expired", cd === "Plan vencido");
    el.classList.remove("no-plan");
    el.onclick = null;
    el.title = "";
  } else {
    el.textContent = "Sin plan activo";
    el.classList.remove("expired");
    el.classList.add("no-plan");
    el.title = "Ver planes";
    el.onclick = () => document.querySelector('[data-tab="plans"]').click();
  }
  $("panelUserPlan").textContent = u.is_admin ? "Admin" : (u.plan && u.plan !== "free" ? u.plan : "Sin plan");
  $("panelUserPlan").classList.toggle("admin", !!u.is_admin);
  // Banner de impersonación (el admin entró a la cuenta de un usuario).
  const imp = localStorage.getItem("clipper_admin_token");
  $("impBanner").hidden = !imp;
  if (imp) $("impName").textContent = localStorage.getItem("clipper_impersonating") || u.name || "";
}
// La cuenta atrás se actualiza sola cada minuto (de "29 días" a
// "23 h 59 min" sin recargar).
setInterval(() => { if (state.user) updateTopBar(); }, 60000);
function doLogout() {
  localStorage.removeItem("clipper_token");
  localStorage.removeItem("clipper_admin_token");
  localStorage.removeItem("clipper_impersonating");
  showUser(null);
  // Al salir se vuelve a la landing, con su URL limpia.
  history.pushState({}, "", "/");
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

// ---------- URLs limpias por sección ----------
// Cada pestaña del panel tiene su propia URL (Juan, 2026-11-10: antes
// todo cargaba como AJAX y la URL nunca cambiaba). Al cambiar de
// pestaña la URL cambia con pushState; atrás/adelante y recargar abren
// la sección de la URL; entrar por un enlace directo abre esa sección
// tras el login. El backend sirve index.html en cada una de estas
// rutas, así que recargar no da 404.
const ROUTES = {
  home: "/inicio", clips: "/mis-clips", create: "/crear-clip",
  lives: "/lives", images: "/imagenes-ia", videos: "/videos-ia",
  plans: "/planes", account: "/mi-cuenta", admin: "/admin",
};
const ROUTE_TABS = Object.fromEntries(
  Object.entries(ROUTES).map(([tab, path]) => [path, tab]));
function tabFromUrl() {
  const p = location.pathname.replace(/\/+$/, "") || "/";
  if (p === "/") return "home";
  return ROUTE_TABS[p] || "home";
}
function goTab(tab, { push = false } = {}) {
  // Una pestaña no disponible (p. ej. admin para un usuario normal)
  // cae a Inicio y su URL.
  let btn = document.querySelector(`.panel .tab[data-tab="${tab}"]`);
  if (!btn || btn.hidden) tab = "home";
  btn = document.querySelector(`.panel .tab[data-tab="${tab}"]`);
  if (!btn) return false;
  document.body.classList.toggle("create-wide", tab === "create");
  document.querySelectorAll(".panel .tab[data-tab]").forEach((b) => b.classList.toggle("on", b === btn));
  document.querySelectorAll(".panel .pane").forEach((p) => p.hidden = true);
  $("pane-" + tab).hidden = false;
  if (tab === "clips") loadMyClips();
  if (tab === "lives") loadLives();
  if (tab === "images") { loadImageModels(); loadMyImages(); }
  if (tab === "videos") { loadVideoModels(); loadMyVideos(); }
  if (tab === "admin") loadAdmin();
  const url = ROUTES[tab] || "/inicio";
  if (push) history.pushState({ tab }, "", url);
  else history.replaceState({ tab }, "", url);
  return true;
}
function openTabFromUrl() {
  goTab(tabFromUrl());
}
window.addEventListener("popstate", () => {
  if (!$("panel").hidden) openTabFromUrl();
});

// ---------- Panel: pestañas ----------
// Al entrar en Crear Clip el menú lateral se cierra (body.create-wide) para
// que la sección se vea más abierta; cualquier otra pestaña, el logo o el
// botón "← Volver al panel" lo devuelven.
document.querySelectorAll(".panel .tab[data-tab]").forEach((btn) => {
  btn.onclick = () => {
    if (btn.dataset.tab === "creator") {
      closePanel();
      history.pushState({}, "", "/");
      return;
    }
    goTab(btn.dataset.tab, { push: true });
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
      earnings: loadAdminEarnings, settings: loadAdminSettings };
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
  // La plantilla guardada manda: aplica su preset a los controles del
  // editor (marco, color, grosor, posición, resaltado). Un nombre
  // viejo o desconocido cae a "Gancho grande".
  const tpl = (u.template && TEMPLATES_JS[u.template]) ? u.template : "Gancho grande";
  $("cfgTemplate").value = tpl;
  state.userTemplate = tpl;
  applyTemplateToControls(tpl);
  $("cfgMaxClips").value = u.max_clips ?? 5;
  $("cfgMinClip").value = u.min_clip_secs ?? 20;
  $("cfgMaxClip").value = u.max_clip_secs ?? 45;
  $("cfgQuality").value = u.quality || "1080p";
}
// Cambiar la plantilla en Mi Cuenta aplica su preset al editor al
// momento (el render que sigue ya la usa; "Guardar configuración" la
// deja guardada en la cuenta).
$("cfgTemplate").addEventListener("change", () => {
  const tpl = $("cfgTemplate").value;
  state.userTemplate = tpl;
  applyTemplateToControls(tpl);
});
// Si el usuario toca un control de la plantilla (marco, color, grosor,
// posición), el render deja de seguir la plantilla y es personalizado
// (template=null). El color de palabra clave NO desengancha la
// plantilla: es una elección por render.
["frameEnabled", "frameColor", "frameWidth", "subPos"]
  .forEach((id) => $(id).addEventListener("input", () => { state.templateTouched = true; }));

async function loadPanel() {
  // ¿Tiene plan activo? Si no, se bloquean Inicio / Mis Clips / Crear / Cuenta.
  // El ADMIN nunca se bloquea: no necesita plan para usar la plataforma.
  let hasPlan = !!(state.user && state.user.is_admin), activePlanId = null;
  // Imágenes IA: solo plan Full (o admin). El backend es la autoridad
  // (403); esto solo decide qué candado se muestra.
  state.canImages = !!(state.user && state.user.is_admin);
  // Videos IA: planes Pro y Full (o admin).
  state.canVideos = !!(state.user && state.user.is_admin);
  try {
    const d = await apiFetch("/api/subscriptions/mine", { headers: authHeaders() });
    hasPlan = hasPlan || !!d.active_subscription;
    activePlanId = d.active_subscription ? d.active_subscription.plan_id : null;
    if (d.active_subscription && d.active_subscription.plan_name === "Full") state.canImages = true;
    if (d.active_subscription && ["Full", "Pro"].includes(d.active_subscription.plan_name)) state.canVideos = true;
    renderMyRequests(d.requests || []);
  } catch { /* sin plan */ }
  ["home", "clips", "create", "account", "images", "videos"].forEach((t) => {
    const pane = $("pane-" + t);
    if (pane) pane.classList.toggle("is-locked", !hasPlan);
  });
  const imgPane = $("pane-images");
  if (imgPane) imgPane.classList.toggle("is-locked-full", hasPlan && !state.canImages);
  const vidPane = $("pane-videos");
  if (vidPane) vidPane.classList.toggle("is-locked-full", hasPlan && !state.canVideos);
  try {
    const s = await apiFetch("/api/me/stats", { headers: authHeaders() });
    $("stMonth").textContent = s.clips_this_month ?? 0;
    $("stProcessing").textContent = s.videos_in_process ?? 0;
    $("stTotalClips").textContent = s.clips_total ?? 0;
    $("stVideos").textContent = s.videos_total ?? 0;
    $("stMinutes").textContent = s.minutes_total ?? 0;
    $("stLives").textContent = s.lives_active ?? 0;
    const recent = $("homeRecent");
    if (recent) {
      recent.innerHTML = "";
      const list = s.recent_clips || [];
      if (!list.length) {
        recent.innerHTML = "<p class='muted'>Aún no tienes clips. Ve a Crear Clip.</p>";
      } else {
        list.forEach((c) => recent.appendChild(clipRow(c, false)));
      }
    }
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
    // Tabla con fechas de la membresía (inicio / fin) y un menú ⋮ por
    // usuario con Asignar plan, Entrar como usuario, Editar, Eliminar
    // y Suspender/Reactivar cuenta.
    const fmtDate = (iso) => (iso ? iso.slice(0, 10) : "—");
    $("adminUsers").innerHTML = "<table><tr><th>Nombre</th><th>Correo</th><th>Plan</th><th>Inicio</th><th>Fin</th><th></th></tr>" +
      users.map((u) => {
        const susp = u.is_suspended ? ` <span class="susp-badge">⛔ Suspendida</span>` : "";
        const adminTag = u.is_admin ? " <small>· Admin</small>" : "";
        // Si ya tiene plan, el menú dice "Cambiar plan"; si no, "Asignar plan".
        const hasPlan = u.plan && u.plan !== "free";
        return `<tr><td>${esc(u.name)}${susp}${adminTag}</td><td>${esc(u.email)}</td><td>${esc(u.plan)}</td><td>${fmtDate(u.plan_started)}</td><td>${fmtDate(u.plan_expires)}</td>
        <td><div class="user-menu-wrap"><button class="small ghost user-menu-btn" data-menu="${u.id}" title="Opciones" aria-label="Opciones">${MENU_SVG}</button>
        <div class="user-menu" id="user-menu-${u.id}" hidden>
          ${u.is_admin ? "" : `<button data-assign="${u.id}" data-name="${esc(u.name)}" data-email="${esc(u.email)}">${hasPlan ? "Cambiar plan" : "Asignar plan"}</button>`}
          ${u.is_admin ? "" : `<button data-imp="${u.id}" data-name="${esc(u.name)}">Entrar como usuario</button>`}
          <button data-edit="${u.id}" data-name="${esc(u.name)}" data-email="${esc(u.email)}">Editar</button>
          <button data-susp="${u.id}" data-name="${esc(u.name)}" data-suspended="${u.is_suspended ? "1" : "0"}">${u.is_suspended ? "Reactivar cuenta" : "Suspender cuenta"}</button>
          <button class="danger" data-del="${u.id}" data-name="${esc(u.name)}">Eliminar</button>
        </div></div></td></tr>`;
      }).join("") + "</table><p id=\"usersMsg\" class=\"muted\"></p>";
    // Menú ⋮: abre uno a la vez; cierra con clic fuera o Escape.
    const closeAllMenus = () => document.querySelectorAll(".user-menu").forEach((m) => { m.hidden = true; });
    $("adminUsers").querySelectorAll("[data-menu]").forEach((b) => b.onclick = (e) => {
      e.stopPropagation();
      const m = $("user-menu-" + b.dataset.menu);
      const wasHidden = m.hidden;
      closeAllMenus();
      m.hidden = !wasHidden;
    });
    document.addEventListener("click", closeAllMenus);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAllMenus(); });
    $("adminUsers").querySelectorAll("[data-imp]").forEach((b) => b.onclick = () => { closeAllMenus(); impersonateUser(b.dataset.imp, b.dataset.name); });
    $("adminUsers").querySelectorAll("[data-assign]").forEach((b) => b.onclick = () => { closeAllMenus(); openAssignModal(b.dataset.assign, b.dataset.name, b.dataset.email, plans); });
    $("adminUsers").querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => { closeAllMenus(); openEditUserModal(b.dataset.edit, b.dataset.name, b.dataset.email); });
    $("adminUsers").querySelectorAll("[data-susp]").forEach((b) => b.onclick = () => { closeAllMenus(); toggleSuspendUser(b.dataset.susp, b.dataset.name, b.dataset.suspended === "1"); });
    $("adminUsers").querySelectorAll("[data-del]").forEach((b) => b.onclick = () => deleteUser(b));
  } catch { $("adminUsers").innerHTML = "<p class='muted'>No se pudieron cargar los usuarios.</p>"; }
}
// Editar usuario (nombre y correo) — modal como el de añadir usuario.
function openEditUserModal(userId, name, email) {
  $("editUserId").value = userId;
  $("editUserName").value = name;
  $("editUserEmail").value = email;
  $("editUserMsg").textContent = "";
  $("editUserModal").hidden = false;
  $("editUserName").focus();
}
function closeEditUserModal() { $("editUserModal").hidden = true; }
// Suspender / reactivar una cuenta.
async function toggleSuspendUser(userId, name, isSuspended) {
  const action = isSuspended ? "unsuspend" : "suspend";
  try {
    await apiFetch(`/api/admin/users/${userId}/${action}`, { method: "POST", headers: authHeaders() });
    await loadAdminUsers();
    const m2 = $("usersMsg");
    if (m2) m2.textContent = isSuspended ? `Cuenta de ${name} reactivada.` : `Cuenta de ${name} suspendida.`;
  } catch (e) {
    const m2 = $("usersMsg");
    if (m2) m2.textContent = "Error: " + e.message;
  }
}
// Eliminar usuario: dos toques para confirmar (sin diálogo nativo).
async function deleteUser(btn) {
  if (btn.dataset.confirm !== "1") {
    btn.dataset.confirm = "1";
    btn.textContent = "¿Seguro? Toca de nuevo";
    setTimeout(() => { btn.dataset.confirm = ""; btn.textContent = "Eliminar"; }, 4000);
    return;
  }
  try {
    await apiFetch(`/api/admin/users/${btn.dataset.del}`, { method: "DELETE", headers: authHeaders() });
    await loadAdminUsers();
    const m2 = $("usersMsg");
    if (m2) m2.textContent = `Usuario ${btn.dataset.name} eliminado.`;
  } catch (e) {
    const m2 = $("usersMsg");
    if (m2) m2.textContent = "Error: " + e.message;
  }
}
// Ganancias: total de ventas aprobadas, próximos pagos (renovaciones
// que vencen en 7 días), usuarios activos y suspendidos.
async function loadAdminEarnings() {
  const box = $("adminEarnings");
  try {
    const d = await apiFetch("/api/admin/earnings", { headers: authHeaders() });
    const fmt = (n) => "RD$" + Number(n || 0).toLocaleString("es-DO");
    const byPlan = Object.entries(d.users_by_plan || {}).map(([p, n]) => `${esc(p)}: ${n}`).join(" · ") || "—";
    const upcoming = (d.upcoming_renewals || []).length
      ? `<table><tr><th>Usuario</th><th>Plan</th><th>Monto</th><th>Vence</th></tr>` +
        d.upcoming_renewals.map((r) => `<tr><td>${esc(r.user_name)}<br><small class="muted">${esc(r.user_email)}</small></td><td>${esc(r.plan_name)}</td><td>${fmt(r.amount_dop)}</td><td>${esc((r.ends_at || "").slice(0, 10))}</td></tr>`).join("") + "</table>"
      : "<p class='muted'>Ninguna membresía vence en los próximos 7 días.</p>";
    const recent = (d.recent_sales || []).length
      ? `<table><tr><th>Usuario</th><th>Plan</th><th>Monto</th><th>Fecha</th></tr>` +
        d.recent_sales.map((r) => `<tr><td>${esc(r.user_name)}<br><small class="muted">${esc(r.user_email)}</small></td><td>${esc(r.plan_name)}</td><td>${fmt(r.amount_dop)}</td><td>${esc((r.decided_at || "").slice(0, 10))}</td></tr>`).join("") + "</table>"
      : "<p class='muted'>Aún no hay ventas aprobadas.</p>";
    const susp = (d.suspended_users || []).length
      ? `<table><tr><th>Usuario</th><th>Correo</th><th>Plan</th></tr>` +
        d.suspended_users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td><td>${esc(u.plan)}</td></tr>`).join("") + "</table>"
      : "<p class='muted'>Ninguna cuenta suspendida.</p>";
    box.innerHTML = `<div class="stats">
        <div class="stat red"><b>${fmt(d.total_sales_dop)}</b><span>Total de ventas (${d.total_sales_count} aprobadas)</span></div>
        <div class="stat"><b>${fmt(d.month_sales_dop)}</b><span>Ventas este mes</span></div>
        <div class="stat"><b>${d.active_users}</b><span>Usuarios activos</span></div>
        <div class="stat"><b>${d.suspended_count}</b><span>Cuentas suspendidas</span></div>
        <div class="stat"><b>${d.total_users}</b><span>Usuarios en total</span></div>
        <div class="stat"><b>${esc(byPlan)}</b><span>Activos por plan</span></div>
      </div>
      <h4 style="margin:18px 0 6px">Próximos pagos (vencen en 7 días)</h4>${upcoming}
      <h4 style="margin:18px 0 6px">Últimas ventas</h4>${recent}
      <h4 style="margin:18px 0 6px">Cuentas suspendidas</h4>${susp}`;
  } catch {
    box.innerHTML = "<p class='muted'>No se pudieron cargar las ganancias.</p>";
  }
}
// Modal "Asignar plan": una tarjeta centrada con el usuario arriba y
// los planes como filas seleccionables (nombre, precio, duración, ⭐).
// Cierra con Cancelar, la ×, clic en el fondo o Escape.
let assignTarget = null;
function openAssignModal(userId, name, email, plans) {
  assignTarget = { userId, name };
  $("assignUserLine").textContent = `${name} · ${email}`;
  $("assignPlanList").innerHTML = plans.map((p, i) =>
    `<label class="assign-plan-row${i === 0 ? " selected" : ""}">
      <input type="radio" name="assignPlan" value="${p.id}"${i === 0 ? " checked" : ""}>
      <span><span class="ap-name">${esc(p.name)}</span>${p.is_popular ? '<span class="ap-pop">⭐ Popular</span>' : ""}<br>
      <span class="ap-meta">RD$ ${p.price_dop.toLocaleString("es-DO")} · ${p.duration_days} días</span></span>
    </label>`).join("") || "<p class='muted'>No hay planes. Crea uno en la pestaña Planes.</p>";
  $("assignPlanList").querySelectorAll(".assign-plan-row").forEach((row) => {
    row.addEventListener("click", () => {
      $("assignPlanList").querySelectorAll(".assign-plan-row").forEach((r) => r.classList.remove("selected"));
      row.classList.add("selected");
    });
  });
  $("assignModal").hidden = false;
}
function closeAssignModal() {
  $("assignModal").hidden = true;
  assignTarget = null;
}
$("assignClose").onclick = closeAssignModal;
$("assignCancel").onclick = closeAssignModal;
$("assignModal").addEventListener("click", (e) => { if (e.target === $("assignModal")) closeAssignModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("assignModal").hidden) closeAssignModal(); });
$("assignConfirm").onclick = async () => {
  if (!assignTarget) return;
  const sel = $("assignPlanList").querySelector('input[name="assignPlan"]:checked');
  if (!sel) return;
  const planId = parseInt(sel.value, 10);
  try {
    const r = await apiFetch(`/api/admin/users/${assignTarget.userId}/assign-plan`, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ plan_id: planId }) });
    const name = assignTarget.name;
    closeAssignModal();
    await loadAdminUsers();
    const m2 = $("usersMsg");
    if (m2) m2.textContent = `Plan ${r.subscription.plan_name} asignado a ${name} hasta ${r.subscription.ends_at.slice(0, 10)}.`;
  } catch (e) {
    const msg = $("usersMsg");
    if (msg) msg.textContent = "Error: " + e.message;
    closeAssignModal();
  }
};
// Modal "Añadir usuario": botón flotante + en Panel Admin > Usuarios.
// Crea un cliente o un administrador (POST /api/admin/users).
function openAddUserModal() {
  $("addUserName").value = "";
  $("addUserEmail").value = "";
  $("addUserPassword").value = "";
  const client = document.querySelector('input[name="addUserRole"][value="client"]');
  if (client) client.checked = true;
  $("addUserMsg").textContent = "";
  $("addUserModal").hidden = false;
  $("addUserName").focus();
}
function closeAddUserModal() { $("addUserModal").hidden = true; }
$("fabAddUser").onclick = openAddUserModal;
$("addUserClose").onclick = closeAddUserModal;
$("addUserCancel").onclick = closeAddUserModal;
$("addUserModal").addEventListener("click", (e) => { if (e.target === $("addUserModal")) closeAddUserModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("addUserModal").hidden) closeAddUserModal(); });
$("addUserConfirm").onclick = async () => {
  const name = $("addUserName").value.trim();
  const email = $("addUserEmail").value.trim();
  const password = $("addUserPassword").value;
  const role = (document.querySelector('input[name="addUserRole"]:checked') || {}).value || "client";
  const msg = $("addUserMsg");
  if (!name || !email || !password) { msg.textContent = "Completa nombre, correo y contraseña."; return; }
  if (password.length < 6) { msg.textContent = "La contraseña debe tener al menos 6 caracteres."; return; }
  msg.textContent = "Creando…";
  try {
    const r = await apiFetch("/api/admin/users", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ name, email, password, is_admin: role === "admin" }) });
    closeAddUserModal();
    await loadAdminUsers();
    const m2 = $("usersMsg");
    if (m2) m2.textContent = `Usuario ${r.name} (${r.email}) creado como ${r.is_admin ? "Administrador" : "Cliente"}.`;
  } catch (e) {
    msg.textContent = "Error: " + e.message;
  }
};
// Editar usuario: guardar nombre y correo.
$("editUserClose").onclick = closeEditUserModal;
$("editUserCancel").onclick = closeEditUserModal;
$("editUserModal").addEventListener("click", (e) => { if (e.target === $("editUserModal")) closeEditUserModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("editUserModal").hidden) closeEditUserModal(); });
$("editUserConfirm").onclick = async () => {
  const id = $("editUserId").value;
  const name = $("editUserName").value.trim();
  const email = $("editUserEmail").value.trim();
  const msg = $("editUserMsg");
  if (!name || !email) { msg.textContent = "Completa nombre y correo."; return; }
  msg.textContent = "Guardando…";
  try {
    await apiFetch(`/api/admin/users/${id}`, { method: "PATCH", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ name, email }) });
    closeEditUserModal();
    await loadAdminUsers();
    const m2 = $("usersMsg");
    if (m2) m2.textContent = `Usuario ${name} actualizado.`;
  } catch (e) {
    msg.textContent = "Error: " + e.message;
  }
};
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
    $("setTemplate").value = (s.default_template && TEMPLATES_JS[s.default_template])
      ? s.default_template : "Gancho grande";
    $("setHighlight").value = s.subtitle_highlight_color || "#FADE4C";
  } catch { $("setMsg").textContent = "No se pudo cargar la configuración."; }
}
$("btnSaveSettings").onclick = async () => {
  try {
    await apiFetch("/api/admin/settings", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ settings: { default_brand_text: $("setBrand").value.trim() || "@marketingcaribe", default_frame_enabled: $("setFrame").checked ? "true" : "false", default_template: $("setTemplate").value, subtitle_highlight_color: $("setHighlight").value } }) });
    $("setMsg").textContent = "Configuración guardada. Los usuarios nuevos recibirán estos valores.";
  } catch (e) { $("setMsg").textContent = "Error: " + e.message; }
};

// Mis Clips como TELÉFONOS: la tarjeta es el celular (marco redondeado
// con bisel + isla dinámica), el video llena la pantalla 9:16, arriba
// el fuego + % de viralidad a la izquierda y la duración a la derecha,
// y abajo el título + dos botones de icono (descargar rojo, lápiz
// oscuro). Sin score guardado no se inventa ningún %: no sale fuego.
const DL_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="M6 11l6 6 6-6"/><path d="M4 21h16"/></svg>';
const EDIT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>';
const PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>';
const MENU_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>';
const TRASH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function clipRow(c, admin) {
  const div = document.createElement("div");
  div.className = "clip-card";
  const dur = fmtDur((c.end || 0) - (c.start || 0));
  const fire = (typeof c.score === "number")
    ? `<span class="fire-badge">🔥 ${Math.round(c.score)}%</span>` : "";
  const thumb = c.download_url
    ? `<video muted playsinline preload="metadata" src="${c.download_url}${typeof c.cover_at === "number" ? "#t=" + c.cover_at : ""}"></video>`
    : `<div class="clip-thumb-empty">Sin video</div>`;
  const date = c.created_at ? c.created_at.slice(0, 10) : "";
  const meta = [c.uploader || "", date].filter(Boolean).join(" · ");
  const who = admin
    ? `<div class="muted small">${esc(c.user_name || "")} · ${esc(c.user_email || "")}</div>` : "";
  div.innerHTML = `<div class="clip-thumb">${thumb}
      <div class="clip-top">${fire}<span class="dur-badge">${dur}</span></div>
      ${c.download_url ? `<button class="clip-play" title="Reproducir" aria-label="Reproducir">${PLAY_SVG}</button>` : ""}
    </div>
    <div class="clip-body">
      <div class="clip-title">${esc(c.title || "Clip")}</div>
      ${meta ? `<div class="muted small">${esc(meta)}</div>` : ""}
      ${who}
      <div class="clip-actions">
        ${c.download_url ? `<a class="icon-btn dl" href="${c.download_url}" download title="Descargar">${DL_SVG}</a>` : ""}
        <button class="icon-btn edit" title="Editar">${EDIT_SVG}</button>
        <button class="icon-btn del" title="Eliminar">${TRASH_SVG}</button>
      </div>
    </div>`;
  div.querySelector(".icon-btn.edit").onclick = () => editClip(c);
  // Eliminar: dos toques para confirmar (sin diálogo nativo). Al
  // confirmar se borra el clip (fila + mp4) y la tarjeta desaparece.
  const delBtn = div.querySelector(".icon-btn.del");
  delBtn.onclick = async () => {
    if (delBtn.dataset.confirm !== "1") {
      delBtn.dataset.confirm = "1";
      delBtn.classList.add("confirm");
      delBtn.title = "Toca de nuevo para confirmar";
      setTimeout(() => { delBtn.dataset.confirm = ""; delBtn.classList.remove("confirm"); delBtn.title = "Eliminar"; }, 4000);
      return;
    }
    try {
      await apiFetch(`/api/me/clips/${c.id}`, { method: "DELETE", headers: authHeaders() });
      div.remove();
    } catch (e) {
      delBtn.dataset.confirm = "";
      delBtn.classList.remove("confirm");
      delBtn.title = "Eliminar";
      alert("No se pudo eliminar: " + e.message);
    }
  };
  const playBtn = div.querySelector(".clip-play");
  if (playBtn) playBtn.onclick = (e) => { e.stopPropagation(); openClipModal(c); };
  div.querySelector(".clip-thumb").onclick = () => openClipModal(c);
  return div;
}

// Reproducir un clip en un modal (con sonido), para verlo sin salir de
// Mis Clips. Al cerrar se pausa el video para que no siga sonando.
let clipModalClip = null;
function openClipModal(c) {
  if (!c.download_url) return;
  clipModalClip = c;
  $("clipModalTitle").textContent = c.title || "Clip";
  const dur = fmtDur((c.end || 0) - (c.start || 0));
  const fire = (typeof c.score === "number") ? `🔥 ${Math.round(c.score)}% · ` : "";
  $("clipModalMeta").textContent = `${fire}${dur}`;
  const v = $("clipModalVideo");
  v.src = c.download_url;
  $("clipModalDl").href = c.download_url;
  $("clipModalDl").setAttribute("download", c.file || "clip.mp4");
  $("clipModal").hidden = false;
  v.play().catch(() => {});
}
function closeClipModal() {
  const v = $("clipModalVideo");
  v.pause();
  v.removeAttribute("src");
  v.load();
  $("clipModal").hidden = true;
  clipModalClip = null;
}
$("clipModalClose").onclick = closeClipModal;
$("clipModal").addEventListener("click", (e) => { if (e.target === $("clipModal")) closeClipModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("clipModal").hidden) closeClipModal(); });
$("clipModalEdit").onclick = () => { const c = clipModalClip; closeClipModal(); if (c) editClip(c); };

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

// ---------- Lives (clips automáticos de un live en curso) ----------
const LIVE_STATUS_LABEL = {
  active: "● En vivo", paused: "⏸ Pausado", stopped: "■ Detenido",
  ended: "✓ Terminado",
};
let livesTimer = null;

async function loadLives() {
  const box = $("myLives");
  try {
    const list = await apiFetch("/api/lives", { headers: authHeaders() });
    box.innerHTML = "";
    if (!list.length) {
      box.innerHTML = "<p class='muted'>Aún no tienes lives. Pega el link de un live en curso arriba y empieza.</p>";
    }
    for (const s of list) {
      const card = document.createElement("div");
      card.className = "live-card";
      const status = LIVE_STATUS_LABEL[s.status] || s.status;
      const platform = (s.platform || "youtube").toLowerCase();
      const platformLabel = platform === "twitch" ? "Twitch" : "YouTube";
      const err = s.last_error ? `<p class="muted" style="color:#f87171;margin:10px 0 0">${esc(s.last_error)}</p>` : "";
      const thumb = s.thumbnail
        ? `<img src="${esc(s.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">`
        : `<div class="live-thumb-empty">📺</div>`;
      const onair = s.status === "active" ? `<span class="live-onair">● EN VIVO</span>` : "";
      card.innerHTML = `<div class="live-head">
          <div class="live-thumb">${thumb}${onair}</div>
          <div class="live-info">
            <div class="live-title">${esc(s.title || "Live")}</div>
            <div class="live-badges">
              <span class="live-badge ${esc(s.status)}">${esc(status)}</span>
              <span class="live-platform ${esc(platform)}">${esc(platformLabel)}</span>
              ${s.uploader ? `<span class="muted" style="font-size:12px">${esc(s.uploader)}</span>` : ""}
            </div>
            <a class="live-url" href="${esc(s.url)}" target="_blank" rel="noopener">🔗 ${esc(s.url)}</a>
          </div>
        </div>
        <p class="live-meta">${s.clips_per_round} clip${s.clips_per_round === 1 ? "" : "s"} cada ${s.interval_minutes} min · ${s.clip_count || 0} clips sacados</p>
        ${err}
        <div class="live-actions">
          ${s.status === "active" ? `<button class="small ghost" data-live-pause="${s.id}">⏸ Pausar</button>` : ""}
          ${s.status === "paused" || s.status === "stopped" ? `<button class="small" data-live-resume="${s.id}">▶ Reanudar</button>` : ""}
          ${s.status === "active" || s.status === "paused" ? `<button class="small ghost" data-live-stop="${s.id}">■ Detener</button>` : ""}
          <button class="small ghost" data-live-del="${s.id}">🗑 Eliminar</button>
        </div>
        <div class="live-clips" id="live-clips-${s.id}" style="margin-top:14px"><p class="muted">Cargando clips…</p></div>`;
      box.appendChild(card);
      // Clips de este live, en la misma sección.
      try {
        const clips = await apiFetch(`/api/lives/${s.id}/clips`, { headers: authHeaders() });
        const grid = card.querySelector(`#live-clips-${s.id}`);
        grid.innerHTML = "";
        if (!clips.length) {
          grid.innerHTML = "<p class='muted'>Todavía no hay clips. El primero aparece cuando se cumpla el primer intervalo.</p>";
        } else {
          clips.forEach((c) => grid.appendChild(clipRow(c, false)));
        }
      } catch {
        const grid = card.querySelector(`#live-clips-${s.id}`);
        if (grid) grid.innerHTML = "<p class='muted'>No se pudieron cargar los clips.</p>";
      }
    }
    box.querySelectorAll("[data-live-pause]").forEach((b) => b.onclick = () => liveControl(b.dataset.livePause, "pause"));
    box.querySelectorAll("[data-live-resume]").forEach((b) => b.onclick = () => liveControl(b.dataset.liveResume, "resume"));
    box.querySelectorAll("[data-live-stop]").forEach((b) => b.onclick = () => liveControl(b.dataset.liveStop, "stop"));
    box.querySelectorAll("[data-live-del]").forEach((b) => b.onclick = () => liveDelete(b));
  } catch {
    box.innerHTML = "<p class='muted'>No se pudieron cargar tus lives.</p>";
  }
  // Refresco mientras la pestaña Lives está abierta (los clips nuevos
  // aparecen solos, sin recargar la página).
  if (livesTimer) clearInterval(livesTimer);
  livesTimer = setInterval(() => {
    if (!$("pane-lives").hidden) loadLives();
  }, 30000);
}

async function liveControl(id, action) {
  try {
    await apiFetch(`/api/lives/${id}/${action}`, { method: "POST", headers: authHeaders() });
    loadLives();
  } catch (e) {
    $("liveMsg").textContent = "Error: " + e.message;
  }
}

async function liveDelete(btn) {
  // Dos toques para confirmar (sin diálogo nativo).
  if (btn.dataset.confirm !== "1") {
    btn.dataset.confirm = "1";
    btn.textContent = "¿Seguro? Toca de nuevo";
    setTimeout(() => { btn.dataset.confirm = ""; btn.textContent = "🗑 Eliminar"; }, 4000);
    return;
  }
  try {
    await apiFetch(`/api/lives/${btn.dataset.liveDel}`, { method: "DELETE", headers: authHeaders() });
    loadLives();
  } catch (e) {
    $("liveMsg").textContent = "Error: " + e.message;
  }
}

$("btnStartLive").onclick = async () => {
  const url = $("liveUrl").value.trim();
  const msg = $("liveMsg");
  if (!url) { msg.textContent = "Pega el link del live."; return; }
  msg.textContent = "Comprobando que el live esté en curso…";
  $("btnStartLive").disabled = true;
  try {
    const d = await apiFetch("/api/lives", {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        url,
        interval_minutes: parseInt($("liveInterval").value || "10", 10),
        clips_per_round: parseInt($("liveClipsPerRound").value || "3", 10),
      }),
    });
    msg.textContent = `Live agregado: ${d.title}. El primer clip aparece en unos ${d.interval_minutes} minutos.`;
    $("liveUrl").value = "";
    loadLives();
  } catch (e) {
    msg.textContent = "Error: " + e.message;
  } finally {
    $("btnStartLive").disabled = false;
  }
};

// ---------- Imágenes IA (fal.ai, plan Full) ----------
const IMG_STYLE_SUFFIX = {
  "Realista": ", estilo realista, fotografía de alta calidad",
  "Dibujo": ", estilo dibujo, ilustración dibujada a mano",
  "3D": ", estilo 3D, render tridimensional",
};
async function loadImageModels() {
  if (state.imageModels) return;
  try {
    state.imageModels = await apiFetch("/api/images/models");
  } catch {
    $("imgModel").innerHTML = "<option value=''>No se pudieron cargar los modelos</option>";
    return;
  }
  $("imgModel").innerHTML = state.imageModels.map((m) =>
    `<option value="${m.id}">${m.name} — ~$${m.price_per_image.toFixed(3)}/imagen</option>`).join("");
  const first = state.imageModels[0];
  $("imgPriceNote").textContent = first && first.approximate ? "Precios aproximados (fal.ai no respondió)." : "";
}
function imgSelectedModel() {
  if (!state.imageModels) return null;
  return state.imageModels.find((m) => m.id === $("imgModel").value) || state.imageModels[0];
}
function stopImgPolling() {
  if (state.imgPollTimer) { clearInterval(state.imgPollTimer); state.imgPollTimer = null; }
}
async function loadMyImages() {
  const box = $("myImages");
  if (!box) return;
  try {
    const list = await apiFetch("/api/images", { headers: authHeaders() });
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<p class='muted'>Aún no tienes imágenes. Describe una arriba y genera la primera.</p>"; return; }
    list.forEach((im) => {
      const div = document.createElement("div");
      div.className = "img-card";
      const done = im.status === "completed" && im.file_url;
      const costTxt = (im.estimated_cost_usd === null || im.estimated_cost_usd === undefined)
        ? "" : ` · ~$${im.estimated_cost_usd.toFixed(3)}`;
      div.innerHTML = `${done ? `<img src="${im.file_url}" alt="" loading="lazy">`
        : `<div class="img-pending">${im.status === "failed" ? "Falló" : "Generando…"}</div>`}
        <div class="img-body"><div class="img-prompt">${esc(im.prompt || "")}</div>
        <div class="img-meta">${esc(imgModelName(im.model))} · ${esc(im.aspect || "")}${costTxt}</div>
        ${im.error ? `<div class="muted" style="font-size:12px">${esc(im.error)}</div>` : ""}
        ${done ? `<a href="${im.file_url}" download><button class="small">⬇ Descargar</button></a>` : ""}</div>`;
      box.appendChild(div);
    });
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar tus imágenes.</p>"; }
}
function imgModelName(id) {
  if (state.imageModels) {
    const m = state.imageModels.find((x) => x.id === id);
    if (m) return m.name;
  }
  return (id || "").split("/").pop() || id || "";
}
$("btnGenImage").onclick = async () => {
  const prompt = $("imgPrompt").value.trim();
  if (!prompt) { $("imgMsg").textContent = "Escribe qué imagen quieres crear."; return; }
  const m = imgSelectedModel();
  if (!m) { $("imgMsg").textContent = "Primero tienen que cargar los modelos."; return; }
  const style = $("imgStyle").value;
  const fullPrompt = prompt + (IMG_STYLE_SUFFIX[style] || "");
  const btn = $("btnGenImage");
  btn.disabled = true;
  btn.textContent = "Generando…";
  $("imgMsg").textContent = "Enviando a fal.ai…";
  $("imgResult").hidden = true;
  stopImgPolling();
  try {
    const d = await apiFetch("/api/images/generate", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ prompt: fullPrompt, model: m.id, aspect: $("imgAspect").value }) });
    $("imgMsg").textContent = `Generando… esto tarda unos segundos. Costo estimado: ~$${m.price_per_image.toFixed(3)}.`;
    loadMyImages();
    // Poll cada 5 s hasta que fal.ai termine (las imágenes tardan segundos).
    state.imgPollTimer = setInterval(async () => {
      try {
        const j = await apiFetch(`/api/images/${d.id}`, { headers: authHeaders() });
        if (j.status === "completed" && j.file_url) {
          stopImgPolling();
          $("imgPreview").src = j.file_url;
          $("imgDownload").href = j.file_url;
          $("imgDownload").download = `imagen-${j.id}.png`;
          $("imgResult").hidden = false;
          $("imgMsg").textContent = "Imagen lista ✓";
          loadMyImages();
        } else if (j.status === "failed") {
          stopImgPolling();
          $("imgMsg").textContent = "Error: " + (j.error || "No se pudo generar la imagen");
          loadMyImages();
        }
      } catch (e) { /* el próximo poll reintenta */ }
    }, 5000);
  } catch (e) {
    $("imgMsg").textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Generar imagen";
  }
};

// ---------- Videos IA (fal.ai, planes Pro/Full) ----------
// Cada generación es UN clip corto (4-15 s según el modelo), no un
// video largo. El costo estimado se muestra ANTES de generar
// ($/s del modelo × duración); el estado final, después.
async function loadVideoModels() {
  if (state.videoModels) { syncVidOptions(); return; }
  try {
    state.videoModels = await apiFetch("/api/videos/models");
  } catch {
    $("vidModel").innerHTML = "<option value=''>No se pudieron cargar los modelos</option>";
    return;
  }
  $("vidModel").innerHTML = state.videoModels.map((m) => {
    const maxDur = Math.max(...m.durations);
    return `<option value="${m.id}">${m.name} — $${m.price_per_second.toFixed(2)}/s · máx. ${maxDur} s${m.audio ? " · con audio" : ""}</option>`;
  }).join("");
  syncVidOptions();
}
function vidSelectedModel() {
  if (!state.videoModels) return null;
  return state.videoModels.find((m) => m.id === $("vidModel").value) || state.videoModels[0];
}
function syncVidOptions() {
  const m = vidSelectedModel();
  if (!m) return;
  $("vidDuration").innerHTML = m.durations.map((d) => `<option value="${d}">${d} s</option>`).join("");
  $("vidDuration").value = String(m.durations.includes(8) ? 8 : m.durations[0]);
  $("vidAspect").innerHTML = m.aspect_ratios.map((a) => `<option value="${a}">${a}</option>`).join("");
  $("vidAspect").value = m.aspect_ratios.includes("9:16") ? "9:16" : m.aspect_ratios[0];
  $("vidAudioRow").style.display = m.audio_param ? "" : "none";
  if (!m.audio_param) $("vidAudio").value = m.audio ? "1" : "0";
  updateVidCost();
}
function updateVidCost() {
  const m = vidSelectedModel();
  if (!m) { $("vidCost").textContent = "Costo estimado: —"; return; }
  const dur = parseInt($("vidDuration").value || "8", 10);
  const rate = m.price_per_second;
  if (rate === null || rate === undefined) {
    $("vidCost").textContent = "Costo estimado: no disponible para este modelo.";
  } else {
    const cost = (rate * dur).toFixed(2);
    $("vidCost").textContent = `Costo estimado: ~$${cost} (${dur} s × $${rate.toFixed(2)}/s)`;
  }
  $("vidPriceNote").textContent = m.approximate ? "Precios aproximados (fal.ai no respondió)." : "";
}
$("vidModel").addEventListener("change", syncVidOptions);
$("vidDuration").addEventListener("change", updateVidCost);
$("vidAspect").addEventListener("change", updateVidCost);
$("vidAudio").addEventListener("change", updateVidCost);
function vidStatusLabel(s) {
  return { processing: "Generando… esto tarda unos minutos",
    completed: "Listo ✓", failed: "Falló" }[s] || s;
}
async function loadMyVideos() {
  const box = $("myVideos");
  if (!box) return;
  try {
    const list = await apiFetch("/api/videos", { headers: authHeaders() });
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<p class='muted'>Aún no tienes videos. Describe uno arriba y genera el primero.</p>"; return; }
    list.forEach((v) => {
      const div = document.createElement("div");
      div.className = "vid-card";
      const done = v.status === "completed" && v.file_url;
      const bad = v.status === "failed";
      const costTxt = (v.estimated_cost_usd === null || v.estimated_cost_usd === undefined)
        ? "" : `~$${v.estimated_cost_usd.toFixed(2)}`;
      div.innerHTML = `${done ? `<video src="${v.file_url}" controls playsinline preload="metadata"></video>` : ""}
        <div class="vid-body"><div class="vid-prompt">${esc(v.prompt || "")}</div>
        <div class="vid-meta">${esc(v.model || "")} · ${v.duration} s · ${esc(v.aspect_ratio || "")} · ${costTxt}</div>
        <span class="vid-status ${done ? "done" : bad ? "bad" : ""}">${vidStatusLabel(v.status)}</span>
        ${v.error ? `<div class="muted" style="font-size:12px">${esc(v.error)}</div>` : ""}
        ${done ? `<p style="margin:10px 0 0"><a href="${v.file_url}" download><button class="small">⬇ Descargar</button></a></p>` : ""}</div>`;
      box.appendChild(div);
    });
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar tus videos.</p>"; }
}
function stopVidPolling() {
  if (state.vidPollTimer) { clearInterval(state.vidPollTimer); state.vidPollTimer = null; }
}
$("btnGenVideo").onclick = async () => {
  const prompt = $("vidPrompt").value.trim();
  if (!prompt) { $("vidMsg").textContent = "Escribe qué video quieres crear."; return; }
  const m = vidSelectedModel();
  if (!m) { $("vidMsg").textContent = "Primero tienen que cargar los modelos."; return; }
  const btn = $("btnGenVideo");
  btn.disabled = true;
  btn.textContent = "Generando…";
  $("vidMsg").textContent = "Enviando a fal.ai…";
  $("vidResult").hidden = true;
  stopVidPolling();
  try {
    const d = await apiFetch("/api/videos/generate", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ prompt, model: m.id, duration: parseInt($("vidDuration").value, 10),
        aspect_ratio: $("vidAspect").value, audio: $("vidAudio").value === "1" }) });
    const costTxt = `~$${d.estimated_cost_usd.toFixed(2)}`;
    $("vidMsg").textContent = `Generando… esto tarda unos minutos. Costo estimado: ${costTxt}.`;
    loadMyVideos();
    // Poll cada 30 s (lo que tarda fal.ai en generarlo) hasta el estado final.
    state.vidPollTimer = setInterval(async () => {
      try {
        const j = await apiFetch(`/api/videos/${d.id}`, { headers: authHeaders() });
        $("vidMsg").textContent = vidStatusLabel(j.status);
        if (j.status === "completed" && j.file_url) {
          stopVidPolling();
          $("vidPreview").src = j.file_url;
          $("vidDownload").href = j.file_url;
          $("vidDownload").download = `video-${j.id}.mp4`;
          $("vidResult").hidden = false;
          $("vidMsg").textContent = "Video listo ✓";
          loadMyVideos();
        } else if (j.status === "failed") {
          stopVidPolling();
          $("vidMsg").textContent = "Error: " + (j.error || vidStatusLabel(j.status));
          loadMyVideos();
        }
      } catch (e) { /* el próximo poll reintenta */ }
    }, 30000);
  } catch (e) {
    $("vidMsg").textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Generar video";
  }
};
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

$("btnChangePassword").onclick = async () => {
  const cur = $("pwCurrent").value;
  const nw = $("pwNew").value;
  const cf = $("pwConfirm").value;
  const msg = $("pwMsg");
  if (!cur || !nw || !cf) { msg.textContent = "Completa los tres campos."; return; }
  if (nw.length < 6) { msg.textContent = "La contraseña nueva debe tener al menos 6 caracteres."; return; }
  if (nw !== cf) { msg.textContent = "La contraseña nueva y la confirmación no coinciden."; return; }
  msg.textContent = "Cambiando…";
  try {
    await apiFetch("/api/auth/change-password", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ current_password: cur, new_password: nw }) });
    $("pwCurrent").value = "";
    $("pwNew").value = "";
    $("pwConfirm").value = "";
    msg.textContent = "Contraseña cambiada. Usa la nueva la próxima vez que entres.";
  } catch (e) { msg.textContent = "Error: " + e.message; }
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
  state.templateTouched = true;
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
        // Sin límite de 300 s: por defecto se analiza el video completo
        // (la transcripción se hace por trozos en el servidor).
        $("rangeEnd").value = info.duration || 120;
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
  // La palabra que suena lleva el color de palabra clave como FONDO
  // (letra negra), igual que en el render; el resto queda en blanco.
  const hl = ($("hlColor") && $("hlColor").value) || "#FADE4C";
  el.innerHTML = g.map((w) => {
    const txt = w.w.trim().toUpperCase();
    return (t >= w.s && t < w.e)
      ? `<span style="background:${hl};color:#000;padding:1px 8px;border-radius:6px">${esc(txt)}</span>`
      : esc(txt);
  }).join(" ");
}
// Cambiar el color actualiza la vista previa al momento (sin esperar
// al siguiente fotograma del video).
$("hlColor").addEventListener("input", () => {
  if (state.pvClip && $("pvVideo")) {
    updatePreviewSubs(Math.max(0, $("pvVideo").currentTime - state.pvClip.start));
  }
});

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

// Sonido de la vista previa: suena por defecto (Juan lo pidió); el botón
// la silencia y la vuelve a activar sin tocar el resto.
$("pvSoundBtn").onclick = () => {
  const pv = $("pvVideo");
  pv.muted = !pv.muted;
  $("pvSoundBtn").textContent = pv.muted ? "🔇 Sonido silenciado" : "🔊 Sonido activado";
};

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
    state.userTemplate = $("cfgTemplate").value;
    applyTemplateToControls(state.userTemplate);
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

// ---------- Guardar título y portada (re-renderiza el clip) ----------
// Cuando se está editando un clip existente, guardar el título y la
// portada RE-RENDERIZA ese mismo clip (mismo id y mismo archivo): el
// título de enganche se quema grande sobre la portada al inicio del
// video, sin marco ni marca. Un PATCH solo cambiaría la miniatura, y
// el título no saldría en el video (eso era lo que estaba mal).
$("btnSaveMeta").onclick = async () => {
  if (!state.editingClipId) return;
  if (!state.clipsJob || state.busy) return;
  const msg = $("saveMetaMsg");
  const start = parseFloat($("clipStart").value), end = parseFloat($("clipEnd").value);
  if (!(end > start)) { msg.textContent = "El fin debe ser mayor que el inicio."; return; }
  msg.textContent = "Guardando y re-renderizando el clip…";
  state.busy = true;
  try {
    const body = buildRenderBody(start, end, null);
    body.update_clip_id = state.editingClipId;
    const d = await apiFetch("/api/render", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    await pollJob(d.job_id, () => {});
    msg.textContent = "Guardado ✓ El título ya sale en la portada del video.";
    loadMyClips();
    if (state.user && state.user.is_admin) loadAdminClips();
  } catch (e) {
    msg.textContent = "Error: " + e.message;
  } finally {
    state.busy = false;
  }
};

// ---------- Cámara (paso 3) ----------
const CAMERA_HINTS = {
  center: "Centrada: el recorte 9:16 fijo en el centro del video, como siempre.",
  follow_speaker: "La cámara sigue a la persona que está hablando. Si no se ve ninguna cara, queda centrada.",
  follow_person: "Sigue siempre a la persona de la zona que elijas. Si no se la ve, queda centrada.",
  dual: "Para entrevistas con dos personas sentadas a izquierda y derecha: la mitad izquierda arriba y la derecha abajo.",
  split: "Para videos de gaming con facecam: la cara del creador arriba y el juego abajo, en dos pantallas.",
};
$("cameraMode").onchange = () => {
  const m = $("cameraMode").value;
  $("cameraPersonRow").hidden = m !== "follow_person";
  $("facecamRow").hidden = m !== "split";
  $("cameraHint").textContent = CAMERA_HINTS[m] || CAMERA_HINTS.center;
};

// ---------- Renderizar ----------
// Cuerpo del render (paso 3): lo comparten el botón Render (clip nuevo)
// y Guardar título y portada (re-render del clip existente).
function buildRenderBody(start, end, edited) {
  return {
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
    facecam_corner: $("facecamCorner").value,
    clip_score: (state.selected && typeof state.selected.score === "number")
      ? state.selected.score : null,
    video_title: state.videoTitle || "",
    show_subtitles: $("showSubs").value === "1",
    subtitle_style: $("subStyle").value,
    subtitle_size: parseInt($("subSize").value || "76", 10),
    subtitle_position: $("subPos").value,
    // Plantilla: null = el usuario tocó un control (render
    // personalizado con sus valores); si no, su plantilla guardada.
    // Color de palabra clave: la elección del editor para este render.
    template: state.templateTouched ? null : (state.userTemplate || "Gancho grande"),
    highlight_color: $("hlColor").value,
    edited_words: edited,
  };
}
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
    const body = buildRenderBody(start, end, edited);
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
