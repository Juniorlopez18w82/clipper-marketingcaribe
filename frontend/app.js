// Clipzio Pro — frontend (same origin, talks to /api/*)
const $ = (id) => document.getElementById(id);
const state = { clipsJob: null, transcript: null, selected: null, words: null,
  user: null, uploadedFile: null, busy: false, pvClip: null,
  // camTrack: recorrido de cámara (crop-x cada 2 s) para la vista
  // previa. La vista previa recorta a 9:16 con CSS centrado y NO
  // refleja "Seguir al que habla": con Masha a la derecha la vista
  // previa la cortaba aunque el render sí la encuadraba. El track
  // viene de /api/jobs/{id}/camera-track y el timeupdate mueve el
  // encuadre con object-position. null = centrado.
  camTrack: null, camTrackKey: "",
  // pvOffset: tiempo (s, relativo a la sección) que corresponde al
  // tiempo 0 del video de la vista previa. Con clip-video (el video es
  // solo el trozo del clip) es el inicio del clip; con la fuente
  // completa es 0. Se usa para convertir tiempos de sección a tiempos
  // del video y viceversa (subtítulos, portada).
  pvOffset: 0,
  editingClipId: null, coverAt: 0, canImages: false, canVideos: false,
  videoModels: null, vidPollTimer: null, imageModels: null, imgPollTimer: null,
  userTemplate: "Gancho grande", templateTouched: false,
  rangeEndTouched: false,
  // Estilo de la portada (modal Configurar portada, 2026-11-11): color
  // del texto, borde de la letra, fondo de las letras y tamaño. Los
  // defaults son lo que ya quemaba el backend: texto blanco, borde
  // negro 3 px, caja negra, tamaño 96. Posición: 0.0 = arriba/izquierda,
  // 0.5 = centrado, 1.0 = abajo/derecha (antes era siempre centrado).
  coverStyle: { color: "#FFFFFF", borderColor: "#000000", borderW: 3,
    bgColor: "#000000", bg: true, size: 96, posX: 0.5, posY: 0.5 } };

// Plantillas (mismo preset que app/render.py: TEMPLATES). El editor
// aplica la plantilla a los controles; si el usuario toca un control de
// la plantilla (marco, color, grosor, posición), el render es
// personalizado y se envía template=null.
const TEMPLATES_JS = {
  "Gancho grande": { frameEnabled: true, frameColor: "#FADE4C", frameWidth: 14, subPos: "bottom", hlColor: "#FADE4C" },
  "Limpio": { frameEnabled: false, frameColor: "#FADE4C", frameWidth: 14, subPos: "bottom", hlColor: "#FADE4C" },
  "Impacto": { frameEnabled: true, frameColor: "#A855F7", frameWidth: 18, subPos: "bottom", hlColor: "#A855F7" },
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
    // Con sesión, al entrar o recargar se abre el PANEL (tu espacio de
    // trabajo). La landing es la puerta para visitantes; se ve con el
    // logo de arriba (brandHome) o cerrando sesión. (2026-11-12, Juan:
    // recargar lo dejaba en la landing y parecía que lo sacaba.)
    openPanel();
    // La nav muestra "Ir a mi panel" en vez de "Entrar" cuando hay sesión.
    const navLogin = $("btnNavLogin");
    if (navLogin) { navLogin.textContent = "Ir a mi panel"; navLogin.onclick = () => openPanel(); }
    const navReg = $("btnNavRegister");
    if (navReg) navReg.hidden = true;
  } else {
    // Sin sesión: se cierra el panel y vuelve la landing; la nav vuelve
    // a mostrar Entrar / Crear cuenta.
    closePanel();
    const navLogin = $("btnNavLogin");
    if (navLogin) { navLogin.textContent = "Entrar"; navLogin.onclick = landingGoToAccount; }
    const navReg = $("btnNavRegister");
    if (navReg) navReg.hidden = false;
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
// ---------- Animaciones al hacer scroll (landing, 2026-11-12) ----------
(function () {
  const els = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) {
    els.forEach((el) => el.classList.add("visible"));
    return;
  }
  // Entrada escalonada dentro de cada grupo de tarjetas.
  const groups = [".steps-grid", ".features-grid", ".landing-plans"];
  groups.forEach((sel) => {
    const grid = document.querySelector(sel);
    if (!grid) return;
    grid.querySelectorAll(".reveal").forEach((card, i) => {
      card.style.transitionDelay = `${i * 0.1}s`;
    });
  });
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("visible"); io.unobserve(en.target); }
    });
  }, { threshold: 0.12 });
  els.forEach((el) => io.observe(el));
})();
// ---------- Landing (2026-11-12, Juan: tipo wayin.ai/clip-maker) ----------
// Si hay sesión, los CTA llevan al panel; si no, bajan al formulario
// de acceso. Los botones de plan llevan al formulario y, al entrar,
// el panel abre solo (showUser ya lo hace).
function landingGoToAccount() {
  if (state.user) { openPanel(); return; }
  $("accountCard").scrollIntoView({ behavior: "smooth", block: "start" });
  $("authEmail").focus({ preventScroll: true });
}
const _landingBtn = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
_landingBtn("btnNavLogin", landingGoToAccount);
_landingBtn("btnNavRegister", landingGoToAccount);
_landingBtn("btnHeroStart", landingGoToAccount);
_landingBtn("btnFinalStart", landingGoToAccount);
_landingBtn("btnHeroPlans", () => { $("planes").scrollIntoView({ behavior: "smooth" }); });
document.querySelectorAll("[data-plan-cta]").forEach((b) => { b.onclick = landingGoToAccount; });
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
  // Video IA e Imagen IA: solo el admin entra directo (2026-11-11,
  // Juan: "Próximamente" para los usuarios). Al resto se le muestra
  // la ventana de Próximamente y no se abre la sección.
  if ((tab === "images" || tab === "videos") && !(state.user && state.user.is_admin)) {
    const name = tab === "images" ? "Las imágenes con IA" : "Los videos con IA";
    $("soonText").textContent = `${name} estarán disponibles próximamente.`;
    $("soonModal").hidden = false;
    return false;
  }
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
$("brandHome").onclick = () => { closePanel(); window.scrollTo({ top: 0, behavior: "smooth" }); };

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
  // Imágenes IA y Videos IA: SOLO admin (2026-11-11, Juan:
  // "Próximamente" para los usuarios; solo el admin entra directo).
  // El backend es la autoridad (403); esto solo decide qué candado
  // se muestra.
  state.canImages = !!(state.user && state.user.is_admin);
  state.canVideos = !!(state.user && state.user.is_admin);
  try {
    const d = await apiFetch("/api/subscriptions/mine", { headers: authHeaders() });
    hasPlan = hasPlan || !!d.active_subscription;
    activePlanId = d.active_subscription ? d.active_subscription.plan_id : null;
    renderMyRequests(d.requests || []);
  } catch { /* sin plan */ }
  // Candado "Próximamente" en el menú para los no-admin (2026-11-11).
  ["images", "videos"].forEach((t) => {
    const btn = document.querySelector(`.panel .tab[data-tab="${t}"]`);
    if (btn) {
      const locked = !(state.user && state.user.is_admin);
      btn.classList.toggle("soon", locked);
      if (locked && !btn.querySelector(".soon-lock")) {
        const s = document.createElement("span");
        s.className = "soon-lock";
        s.textContent = "🔒";
        btn.appendChild(s);
      }
      if (!locked) btn.querySelector(".soon-lock")?.remove();
    }
  });
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
          ${u.is_admin ? "" : `<button data-assign="${u.id}" data-name="${esc(u.name)}" data-email="${esc(u.email)}">${ICON_PLAN}<span>${hasPlan ? "Cambiar plan" : "Asignar plan"}</span></button>`}
          ${u.is_admin ? "" : `<button data-imp="${u.id}" data-name="${esc(u.name)}">${ICON_IMP}<span>Entrar como usuario</span></button>`}
          <button data-edit="${u.id}" data-name="${esc(u.name)}" data-email="${esc(u.email)}">${ICON_EDIT}<span>Editar</span></button>
          <button data-susp="${u.id}" data-name="${esc(u.name)}" data-suspended="${u.is_suspended ? "1" : "0"}">${u.is_suspended ? ICON_PLAY : ICON_PAUSE}<span>${u.is_suspended ? "Reactivar cuenta" : "Suspender cuenta"}</span></button>
          <button class="danger" data-del="${u.id}" data-name="${esc(u.name)}">${ICON_TRASH}<span>Eliminar</span></button>
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
// Iconos del menú ⋮ de usuarios (2026-11-11, petición de Juan: los
// botones del menú iban sin icono y pegados). Stroke currentColor para
// que hereden el color del texto (rojo en Eliminar).
const ICON_PLAN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>';
const ICON_IMP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>';
const ICON_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';
const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="6 3 20 12 6 21 6 3"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
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
  // La miniatura es la PORTADA configurada (2026-11-11): si el clip tiene
  // título de enganche, el video empieza con la tarjeta de portada (2 s
  // con el fotograma + el título), así que la miniatura es el fotograma
  // a 1 s, en medio de la portada. Si no tiene título, no hay tarjeta y
  // la miniatura es el fotograma elegido con cover_at. Antes se usaba
  // siempre #t=cover_at, que con tarjeta mostraba un fotograma del clip
  // (y desplazado 2 s) en vez de la portada.
  const hasTitleCard = !!(c.title && String(c.title).trim());
  const thumbTime = hasTitleCard ? 1
    : (typeof c.cover_at === "number" ? c.cover_at : 0);
  const thumb = c.download_url
    ? `<video muted playsinline preload="metadata" src="${c.download_url}#t=${thumbTime}"></video>`
    : `<div class="clip-thumb-empty">Sin video</div>`;
  const date = c.created_at ? c.created_at.slice(0, 10) : "";
  const meta = [c.uploader || "", date].filter(Boolean).join(" · ");
  const who = admin
    ? `<div class="muted small">${esc(c.user_name || "")} · ${esc(c.user_email || "")}</div>` : "";
  div.innerHTML = `<div class="clip-thumb">${thumb}
      <input type="checkbox" class="clip-select" title="Seleccionar" aria-label="Seleccionar clip">
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
  // Eliminar: abre una ventana de confirmación (2026-11-11, Juan).
  // Al confirmar se borra el clip (fila + mp4) y la tarjeta desaparece.
  const delBtn = div.querySelector(".icon-btn.del");
  delBtn.onclick = () => openConfirmDelete(c, div);
  // Selección masiva (2026-11-12, Juan): el checkbox marca el clip;
  // en modo selección, tocar la tarjeta marca en vez de reproducir.
  const selBox = div.querySelector(".clip-select");
  selBox.checked = _bulkSelected.has(c.id);
  if (_bulkSelected.has(c.id)) div.classList.add("selected");
  if (_bulkSelectMode) div.classList.add("select-mode");
  selBox.onclick = (e) => { e.stopPropagation(); toggleBulkSelect(c.id, div, selBox.checked); };
  const playBtn = div.querySelector(".clip-play");
  if (playBtn) playBtn.onclick = (e) => { e.stopPropagation(); if (_bulkSelectMode) { selBox.checked = !selBox.checked; toggleBulkSelect(c.id, div, selBox.checked); } else openClipModal(c); };
  div.querySelector(".clip-thumb").onclick = () => { if (_bulkSelectMode) { selBox.checked = !selBox.checked; toggleBulkSelect(c.id, div, selBox.checked); } else openClipModal(c); };
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

// ---------- Confirmar eliminación de un clip (ventana, 2026-11-11) ----------
let _confirmDeleteTarget = null; // {clip, cardEl}
function openConfirmDelete(c, cardEl) {
  _confirmDeleteTarget = { clip: c, cardEl };
  $("confirmDeleteText").textContent = `¿Seguro que quieres eliminar "${c.title || "este clip"}"? Esta acción no se puede deshacer.`;
  $("confirmDeleteModal").hidden = false;
}
function closeConfirmDelete() {
  $("confirmDeleteModal").hidden = true;
  _confirmDeleteTarget = null;
}
$("confirmDeleteClose").onclick = closeConfirmDelete;
$("confirmDeleteCancel").onclick = closeConfirmDelete;
$("confirmDeleteModal").addEventListener("click", (e) => { if (e.target === $("confirmDeleteModal")) closeConfirmDelete(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("confirmDeleteModal").hidden) closeConfirmDelete(); });
$("confirmDeleteOk").onclick = async () => {
  const t = _confirmDeleteTarget;
  if (!t) return closeConfirmDelete();
  closeConfirmDelete();
  try {
    if (t.bulk) {
      // Borrado masivo (2026-11-12): un solo llamado al servidor con
      // todos los ids; se quitan de la lista local y se repinta la página.
      const ids = [..._bulkSelected];
      const res = await apiFetch("/api/me/clips/bulk-delete", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ clip_ids: ids }) });
      const gone = new Set(res.deleted || ids);
      _myClips = _myClips.filter((c) => !gone.has(c.id));
      setBulkSelectMode(false);
      renderMyClipsPage();
      if ((res.not_found || []).length) alert(`Se eliminaron ${gone.size} clips. ${(res.not_found || []).length} ya no existían.`);
    } else {
      await apiFetch(`/api/me/clips/${t.clip.id}`, { method: "DELETE", headers: authHeaders() });
      if (t.cardEl) t.cardEl.remove();
    }
  } catch (e) {
    alert("No se pudo eliminar: " + e.message);
  }
};

// ---------- Ventana "Próximamente" (Video IA / Imagen IA) ----------
$("soonClose").onclick = () => { $("soonModal").hidden = true; };
$("soonOk").onclick = () => { $("soonModal").hidden = true; };
$("soonModal").addEventListener("click", (e) => { if (e.target === $("soonModal")) $("soonModal").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("soonModal").hidden) $("soonModal").hidden = true; });

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

// ---------- Mis Clips (con paginación, 2026-11-11, Juan) ----------
const CLIPS_PER_PAGE = 12;
let _myClips = [], _myClipsPage = 0;
function renderMyClipsPage() {
  const box = $("myClips");
  const pages = Math.max(1, Math.ceil(_myClips.length / CLIPS_PER_PAGE));
  if (_myClipsPage >= pages) _myClipsPage = pages - 1;
  if (_myClipsPage < 0) _myClipsPage = 0;
  box.innerHTML = "";
  const start = _myClipsPage * CLIPS_PER_PAGE;
  _myClips.slice(start, start + CLIPS_PER_PAGE).forEach((c) => box.appendChild(clipRow(c, false)));
  const pg = $("clipsPagination");
  if (pg) {
    pg.innerHTML = "";
    if (_myClips.length > CLIPS_PER_PAGE) {
      const prev = document.createElement("button");
      prev.className = "small ghost";
      prev.textContent = "← Anterior";
      prev.disabled = _myClipsPage === 0;
      prev.onclick = () => { _myClipsPage--; renderMyClipsPage(); };
      const info = document.createElement("span");
      info.className = "muted";
      info.style.cssText = "align-self:center;font-size:13px";
      info.textContent = `Página ${_myClipsPage + 1} de ${pages} · ${_myClips.length} clips`;
      const next = document.createElement("button");
      next.className = "small ghost";
      next.textContent = "Siguiente →";
      next.disabled = _myClipsPage >= pages - 1;
      next.onclick = () => { _myClipsPage++; renderMyClipsPage(); };
      pg.append(prev, info, next);
    }
  }
}
async function loadMyClips() {
  const box = $("myClips");
  try {
    _myClips = await apiFetch("/api/me/clips", { headers: authHeaders() });
    if (!_myClips.length) {
      box.innerHTML = "<p class='muted'>Aún no tienes clips. Ve a Crear Clip.</p>";
      const pg = $("clipsPagination");
      if (pg) pg.innerHTML = "";
      return;
    }
    _myClipsPage = 0;
    renderMyClipsPage();
  } catch { box.innerHTML = "<p class='muted'>No se pudieron cargar tus clips.</p>"; }
}

// ---------- Eliminar clips en masa (2026-11-12, Juan) ----------
let _bulkSelectMode = false;
let _bulkSelected = new Set(); // ids de clips seleccionados
function toggleBulkSelect(id, cardEl, checked) {
  if (checked) _bulkSelected.add(id);
  else _bulkSelected.delete(id);
  if (cardEl) cardEl.classList.toggle("selected", checked);
  updateBulkBar();
}
function updateBulkBar() {
  const n = _bulkSelected.size;
  $("bulkCount").textContent = `${n} seleccionado${n === 1 ? "" : "s"}`;
  $("bulkDeleteBtn").disabled = n === 0;
  $("bulkDeleteBtn").textContent = `🗑 Eliminar seleccionados${n ? ` (${n})` : ""}`;
  const all = $("bulkSelectAll");
  if (all) all.checked = _myClips.length > 0 && _bulkSelected.size === _myClips.length;
}
function setBulkSelectMode(on) {
  _bulkSelectMode = on;
  if (!on) _bulkSelected.clear();
  $("bulkBar").hidden = !on;
  $("bulkToggleBtn").textContent = on ? "✕ Salir de selección" : "☑ Seleccionar";
  updateBulkBar();
  renderMyClipsPage();
}
$("bulkToggleBtn").onclick = () => setBulkSelectMode(!_bulkSelectMode);
$("bulkCancelBtn").onclick = () => setBulkSelectMode(false);
$("bulkSelectAll").onchange = (e) => {
  if (e.target.checked) _myClips.forEach((c) => _bulkSelected.add(c.id));
  else _bulkSelected.clear();
  updateBulkBar();
  renderMyClipsPage();
};
$("bulkDeleteBtn").onclick = () => {
  const n = _bulkSelected.size;
  if (!n) return;
  openConfirmBulkDelete(n);
};
function openConfirmBulkDelete(n) {
  _confirmDeleteTarget = { bulk: true, count: n };
  $("confirmDeleteText").textContent = `¿Seguro que quieres eliminar ${n} clip${n === 1 ? "" : "s"}? Esta acción no se puede deshacer.`;
  $("confirmDeleteModal").hidden = false;
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
            <div class="live-title"><a href="${esc(s.url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${esc(s.title || "Live")}</a></div>
            <div class="live-badges">
              <span class="live-badge ${esc(s.status)}">${esc(status)}</span>
              <span class="live-platform ${esc(platform)}">${esc(platformLabel)}</span>
              ${s.uploader ? `<span class="muted" style="font-size:12px">${esc(s.uploader)}</span>` : ""}
            </div>
            <p class="live-meta">${s.clips_per_round} clip${s.clips_per_round === 1 ? "" : "s"} cada ${s.interval_minutes} min · ${s.clip_count || 0} clips</p>
          </div>
        </div>
        ${err}
        <div class="live-actions">
          ${s.status === "active" ? `<button class="icon-btn" data-live-pause="${s.id}" title="Pausar">⏸</button>` : ""}
          ${s.status === "paused" || s.status === "stopped" ? `<button class="icon-btn" data-live-resume="${s.id}" title="Reanudar">▶</button>` : ""}
          ${s.status === "active" || s.status === "paused" ? `<button class="icon-btn" data-live-stop="${s.id}" title="Detener">■</button>` : ""}
          <button class="icon-btn del" data-live-del="${s.id}" title="Eliminar">🗑</button>
        </div>
        <div class="live-clips" id="live-clips-${s.id}" style="margin-top:14px"><p class="muted">Cargando clips…</p></div>`;
      box.appendChild(card);
      // Clips de este live, en la misma sección.
      try {
        const clips = await apiFetch(`/api/lives/${s.id}/clips`, { headers: authHeaders() });
        const grid = card.querySelector(`#live-clips-${s.id}`);
        grid.innerHTML = "";
        if (!clips.length) {
          grid.innerHTML = "<p class='muted'>Sin clips todavía. El primero llega en ~" + s.interval_minutes + " min.</p>";
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
  // Dos toques para confirmar: el botón se pone rojo en el 1er toque.
  if (btn.dataset.confirm !== "1") {
    btn.dataset.confirm = "1";
    btn.classList.add("confirm");
    btn.title = "Toca de nuevo para confirmar";
    setTimeout(() => { btn.dataset.confirm = ""; btn.classList.remove("confirm"); btn.title = "Eliminar"; }, 4000);
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
    msg.textContent = `✓ Live agregado. El primer clip llega en ~${d.interval_minutes} min.`;
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

// Pestañas de Mi Cuenta: Perfil / Configuración (2026-11-11, Juan).
// En Configuración sale la membresía que tiene, cuándo se vence y el
// botón para actualizarla ahora mismo.
document.querySelectorAll("#accTabs .mini-tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#accTabs .mini-tab").forEach((b) => b.classList.toggle("on", b === btn));
    $("accPane-profile").hidden = btn.dataset.acc !== "profile";
    $("accPane-config").hidden = btn.dataset.acc !== "config";
    if (btn.dataset.acc === "config") renderAccPlan();
  };
});
$("btnUpgradePlan").onclick = () => goTab("plans", { push: true });
async function renderAccPlan() {
  const box = $("accPlanBox");
  if (!box) return;
  try {
    const d = await apiFetch("/api/subscriptions/mine", { headers: authHeaders() });
    const sub = d.active_subscription;
    const u = state.user || {};
    if (u.is_admin) {
      box.innerHTML = `<div class="rowline"><div><b>Admin</b><br><small class="muted">Acceso total, sin vencimiento.</small></div><span class="badge aprobado">Admin</span></div>`;
      return;
    }
    if (!sub) {
      box.innerHTML = `<div class="rowline"><div><b>Sin plan activo</b><br><small class="muted">Contrata un plan para usar Clipzio Pro.</small></div><span class="badge">Sin plan</span></div>`;
      return;
    }
    const exp = sub.ends_at ? new Date(sub.ends_at) : null;
    const days = exp ? Math.max(0, Math.ceil((exp - Date.now()) / 86400000)) : null;
    const when = exp ? exp.toLocaleDateString("es-DO", { day: "numeric", month: "long", year: "numeric" }) : "—";
    box.innerHTML = `<div class="rowline"><div><b>Plan ${esc(sub.plan_name || "")}</b><br><small class="muted">Se vence el ${when}${days !== null ? ` · quedan ${days} día${days === 1 ? "" : "s"}` : ""}</small></div><span class="badge aprobado">${esc(sub.plan_name || "Activo")}</span></div>`;
  } catch {
    box.innerHTML = "<p class='muted'>No se pudo cargar tu membresía.</p>";
  }
}

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
  // Tiempo que lleva el trabajo (2026-11-11, Juan: que se vea el
  // tiempo real). Se muestra bajo el estado y se actualiza en cada
  // sondeo (cada 2 s).
  const _jobT0 = Date.now();
  const _elapsedEl = $("jobElapsed");
  const _fmtElapsed = () => {
    const s = Math.floor((Date.now() - _jobT0) / 1000);
    return `⏱ ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  if (_elapsedEl) { _elapsedEl.hidden = false; _elapsedEl.textContent = _fmtElapsed(); }
  try {
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
    if (_elapsedEl) _elapsedEl.textContent = _fmtElapsed();
    if (onProgress) onProgress(j);
    if (j.status === "done") return j;
    if (j.status === "error") {
      const err = new Error("JOB_FAILED");
      err.raw = j.error || "";
      throw err;
    }
    await sleep(2000);
  }
  } finally {
    if (_elapsedEl) _elapsedEl.hidden = true;
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
  // El número junto a la barra (v52): el % real del trabajo, no solo la barra.
  const pct = $("jobPct");
  if (pct) pct.textContent = Math.round(Math.max(0, Math.min(100, p || 0))) + "%";
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
// "Hasta" lo escribe el usuario a mano: si lo toca, no lo pisamos al
// cargar un video; si pega otro link o sube otro archivo, se resetea
// para que el nuevo video ponga su duración completa.
$("url").addEventListener("input", () => { state.rangeEndTouched = false; });
// Miniatura + título al pegar el link (2026-11-11, Juan: que se vea el
// video de una vez). Espera 800 ms tras dejar de escribir para no
// llamar al servidor por cada letra; si el link no es válido o
// YouTube lo bloquea, la vista previa se esconde sin molestar.
let _urlPreviewTimer = null;
$("url").addEventListener("input", () => {
  clearTimeout(_urlPreviewTimer);
  const url = $("url").value.trim();
  if (!url || !/youtube\.com|youtu\.be/.test(url)) {
    $("urlPreview").hidden = true;
    return;
  }
  _urlPreviewTimer = setTimeout(async () => {
    if ($("url").value.trim() !== url) return;
    try {
      const info = await apiFetch("/api/video-info", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ url }) });
      if ($("url").value.trim() !== url) return;
      const mins = info.duration ? `${Math.floor(info.duration / 60)}:${String(info.duration % 60).padStart(2, "0")}` : "live";
      $("urlPreviewThumb").src = info.thumbnail || "";
      $("urlPreviewTitle").textContent = info.title || url;
      $("urlPreviewMeta").textContent = `${info.uploader || ""} · ${mins}`.replace(/^ · /, "");
      $("urlPreview").hidden = false;
      state.videoTitle = info.title || "";
      if (!state.rangeEndTouched && info.duration) $("rangeEnd").value = info.duration;
    } catch (e) {
      $("urlPreview").hidden = true;
    }
  }, 800);
});
$("rangeEnd").addEventListener("input", () => { state.rangeEndTouched = true; });
$("btnUpload").onclick = () => $("uploadFile").click();
$("uploadFile").onchange = async () => {
  const file = $("uploadFile").files[0];
  if (!file) return;
  hidePreview();
  // Archivo nuevo: el "Hasta" viejo no vale; se resetea para que se
  // ponga la duración real de este archivo (antes quedaba en 120 s
  // porque la rama de subida nunca tocaba el campo).
  state.rangeEndTouched = false;
  state.uploadedFile = null;
  // Duración real del archivo, leída en el navegador sin subir nada
  // extra: un <video> temporal con el archivo como fuente.
  const probe = document.createElement("video");
  probe.preload = "metadata";
  probe.src = URL.createObjectURL(file);
  probe.onloadedmetadata = () => {
    URL.revokeObjectURL(probe.src);
    if (!state.rangeEndTouched && isFinite(probe.duration) && probe.duration > 0) {
      $("rangeEnd").value = Math.floor(probe.duration);
      const mins = `${Math.floor(probe.duration / 60)}:${String(Math.floor(probe.duration % 60)).padStart(2, "0")}`;
      $("videoMeta").textContent = `${file.name} · ${mins}`;
    }
  };
  const mb = (file.size / (1024 * 1024)).toFixed(1);
  const info = $("uploadInfo");
  info.hidden = false;
  info.textContent = `Subiendo ${file.name} (${mb} MB)…`;
  const fd = new FormData();
  fd.append("file", file);
  try {
    // Subida con XMLHttpRequest (v52): fetch no reporta el progreso
    // de subida; upload.onprogress muestra los MB reales y el %.
    const d = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      const headers = authHeaders();
      for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const done = (e.loaded / (1024 * 1024)).toFixed(1);
          const total = (e.total / (1024 * 1024)).toFixed(1);
          info.textContent = `Subiendo ${file.name} · ${done} de ${total} MB (${Math.round((e.loaded / e.total) * 100)}%)`;
        }
      };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* no-JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
        reject(new Error((data && data.detail) || FRIENDLY_ERROR));
      };
      xhr.onerror = () => reject(new Error(FRIENDLY_ERROR));
      xhr.send(fd);
    });
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
      // No pisar el "Hasta" que el usuario escribió a mano: solo poner
      // la duración completa si no lo ha tocado, y recortarlo a la
      // duración real si lo tocó. Antes se usaba "si está vacío", pero
      // el campo trae 120 por defecto y no se distinguía del 120
      // escrito por el usuario, así que quedaba capado en 120 s.
      if (!state.rangeEndTouched && info.duration) {
        // Sin límite de 300 s: por defecto se analiza el video completo
        // (la transcripción se hace por trozos en el servidor).
        $("rangeEnd").value = info.duration;
      } else if (state.rangeEndTouched && info.duration) {
        $("rangeEnd").value = Math.min(parseFloat($("rangeEnd").value || 0), info.duration);
      }
      body = { url };
    }
    body.start = parseFloat($("rangeStart").value || 0);
    body.end = parseFloat($("rangeEnd").value || 120);
    body.model = $("model").value;
    body.max_clips = parseInt($("cfgMaxClips").value || "5", 10);
    body.min_len = parseFloat($("cfgMinClip").value || "20");
    body.max_len = parseFloat($("cfgMaxClip").value || "45");
    // El rango debe dar para al menos un clip: si es más corto que la
    // duración mínima del clip no sale ningún candidato y el trabajo se
    // pierde. Se avisa ANTES de arrancar, no con el error genérico.
    if (!(body.end > body.start) || (body.end - body.start) < body.min_len) {
      showJobError(`El rango es muy corto: de ${body.start} s a ${body.end} s son ${Math.max(0, body.end - body.start)} s y cada clip dura al menos ${body.min_len} s. Pon un Hasta mayor (por defecto se analiza el video completo).`);
      return;
    }
    // El servidor acepta hasta 60 min por análisis (2026-11-11): un
    // video más largo se analiza por partes con Desde/Hasta.
    if ((body.end - body.start) > 3600) {
      showJobError(`El video dura ${fmtDur(body.end - body.start)} y el análisis cubre hasta 60 min por vez. Pon un Hasta de 60 min o menos, o analiza el video por partes con Desde/Hasta.`);
      return;
    }
    const d = await apiFetch("/api/clips", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    const job = await pollJob(d.job_id, (j) => {
      setJobProgress(j.progress || 0);
      // El mensaje EN VIVO del servidor (2026-11-11, Juan: la carga
      // debe verse real): durante la descarga lleva partes, MB y
      // velocidad; si el servidor no manda mensaje, cae al texto fijo
      // del paso.
      const step = clipsStepFromProgress(j.progress || 0);
      renderSteps(step, step, (j.message && String(j.message).trim()) || FLOW_MSGS[step]);
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
      // Tarjeta del clip que eligió la IA (2026-11-11): SIN descripción
      // (el texto transcrito ya no se muestra). Solo el video, el botón
      // de vista previa, el rango en minutos y el % de viralización.
      const fire = (typeof c.score === "number")
        ? `<span class="clip-score">🔥 ${Math.round(c.score)}%</span>` : "";
      div.innerHTML = `<video muted playsinline preload="metadata" src="/api/jobs/${job.id}/clip-video?start=${c.abs_start}&end=${c.abs_end}#t=0.1"></video>`
        + `<div class="clip-body"><div class="clip-title">${esc(c.title || `Clip ${i + 1}`)}</div>`
        + `<div class="clip-meta">⏱ ${fmtDur(c.abs_start)} – ${fmtDur(c.abs_end)} ${fire}</div>`
        + `<button type="button" class="cand-preview">▶ Ver vista previa</button></div>`;
      const pick = () => { state.editingClipId = null; selectClip(c, div); };
      div.onclick = pick;
      div.querySelector(".cand-preview").onclick = (e) => { e.stopPropagation(); pick(); };
      box.appendChild(div);
    });
    $("candidatesCard").hidden = false;
    $("candidatesCard").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    console.error("Sacar clips falló:", e.raw || e.message);
    renderSteps(0, -1, "");
    // Muestra el motivo real cuando el servidor lo explica en claro
    // (por ejemplo, el rango es muy largo); si no, el mensaje genérico.
    const raw = (e.raw || "").trim();
    const friendly = raw && raw.length <= 200 && !/traceback|yt-dlp|Error:/i.test(raw)
      ? raw : "No se pudo procesar el video. Revisa el enlace o el archivo e inténtalo de nuevo.";
    showJobError(friendly);
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

// ---------- Recorrido de cámara en la vista previa ----------
// La vista previa recorta el video a 9:16 con CSS (object-fit: cover,
// centrado) y NO reflejaba el modo de cámara: con "Seguir al que habla"
// la persona que habla está a un lado y la vista previa la mostraba
// cortada, aunque el clip renderizado sí la encuadraba bien
// (comprobado 2026-11-11 con Masha: el render con follow_speaker la
// centra completa). El backend calcula el mismo recorrido que usa el
// render (GET /api/jobs/{id}/camera-track) y aquí se mueve el encuadre
// de la vista previa con object-position mientras suena el clip:
// crop-x (px) -> % con x / (width - crop_w). Solo para follow_speaker
// y follow_person; el resto queda centrado (50%).
async function loadCamTrack() {
  const pv = $("pvVideo");
  const key = [
    state.clipsJob && state.clipsJob.id,
    state.selected && state.selected.abs_start,
    state.selected && state.selected.abs_end,
    $("cameraMode") && $("cameraMode").value,
    $("cameraPerson") && $("cameraPerson").value,
  ].join("|");
  state.camTrackKey = key;
  const mode = $("cameraMode") && $("cameraMode").value;
  if (!pv || !state.clipsJob || !state.selected
      || (mode !== "follow_speaker" && mode !== "follow_person")) {
    state.camTrack = null;
    if (pv) pv.style.objectPosition = "50% 50%";
    return;
  }
  try {
    const person = ($("cameraPerson") && $("cameraPerson").value) || "center";
    const r = await fetch(`/api/jobs/${state.clipsJob.id}/camera-track?start=${state.selected.abs_start}&end=${state.selected.abs_end}&mode=${mode}&person=${person}`);
    if (!r.ok) throw new Error("track " + r.status);
    const track = await r.json();
    if (state.camTrackKey !== key) return; // el usuario ya cambió de clip
    state.camTrack = (track && !track.fallback) ? track : null;
    if (!state.camTrack && pv) pv.style.objectPosition = "50% 50%";
  } catch (e) {
    state.camTrack = null;
    if (pv) pv.style.objectPosition = "50% 50%";
  }
}
function applyCamTrack(t) {
  const pv = $("pvVideo");
  if (!pv) return;
  const tr = state.camTrack;
  if (!tr || !tr.path || !tr.path.length || !tr.width || !tr.crop_w) {
    pv.style.objectPosition = "50% 50%";
    return;
  }
  // Muestra del recorrido vigente a este tiempo (pasos de 2 s, como el render).
  let x = tr.path[0][1];
  for (const [pt, px] of tr.path) { if (pt <= t) x = px; else break; }
  const span = tr.width - tr.crop_w;
  const pct = span > 0 ? Math.max(0, Math.min(100, (x / span) * 100)) : 50;
  pv.style.objectPosition = pct.toFixed(1) + "% 50%";
}

// Reproduce el trozo del clip elegido en la vista previa 9:16 (sin sonido).
// El video es SOLO el trozo del clip (clip-video, 2026-11-11): el análisis
// baja solo audio (rápido) y el video se baja bajo demanda para este
// clip. Por eso el tiempo 0 del video es el inicio del clip
// (pvOffset = c.start) y no el inicio de la sección.
function playPreviewClip(c) {
  const pv = $("pvVideo");
  if (!pv || !state.clipsJob) return;
  // Al elegir otro clip se vuelve a la vista normal (marco + marca +
  // subtítulos); la portada se ve con su botón o moviendo el control.
  setCoverMode(false);
  const src = `/api/jobs/${state.clipsJob.id}/clip-video?start=${c.abs_start}&end=${c.abs_end}`;
  state.pvClip = { start: c.start, end: c.end };
  state.pvOffset = c.start;
  // El encuadre de la vista previa sigue al que habla (si el modo es
  // follow_speaker / follow_person); si no, queda centrado.
  loadCamTrack();
  const startAt = () => {
    // Si el usuario ya eligió otro clip mientras cargaban los metadatos,
    // este arranque queda obsoleto.
    if (!state.pvClip || state.pvClip.start !== c.start) return;
    try { pv.currentTime = Math.max(0, c.start - state.pvOffset + 0.01); } catch (e) { /* aún sin metadatos */ }
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
// ---------- Vista previa de la PORTADA (título de enganche) ----------
// La portada real es SOLO el fotograma + el texto gancho: sin marco
// amarillo, sin marca @, sin subtítulos (eso es lo que quema el
// backend en la tarjeta de 2 s). La vista previa normal muestra el clip
// con marco, marca y subtítulos, así que al elegir la portada se veía
// el subtítulo encima — justo lo que Juan dijo que no debe salir.
// Modo portada: pausa el video en el fotograma elegido, esconde marco /
// marca / subtítulos y muestra el texto gancho grande y centrado, igual
// que quedará en el video. Se activa con el botón "Ver portada", al
// mover el control Portada o al escribir el título de enganche, y se
// actualiza EN VIVO mientras se escribe.
function coverTitleText() {
  const typed = ($("hookTitle") && $("hookTitle").value || "").trim();
  if (typed) return typed;
  if (state.selected && state.selected.title) return state.selected.title;
  return "Título de enganche";
}
function updateCoverTitle() {
  const el = $("pvCoverTitle");
  if (el) {
    el.textContent = coverTitleText();
    // Misma posición que en el modal y que en el video (posX/posY).
    el.style.left = (state.coverStyle.posX * 100) + "%";
    el.style.right = "auto";
    el.style.top = (state.coverStyle.posY * 100) + "%";
    el.style.transform = `translate(${-state.coverStyle.posX * 100}%, ${-state.coverStyle.posY * 100}%)`;
  }
}
function setCoverMode(on) {
  const phone = $("phone");
  if (!phone) return;
  phone.classList.toggle("cover-mode", !!on);
  const btn = $("pvCoverBtn");
  if (btn) btn.textContent = on ? "▶ Ver clip" : "🖼 Ver portada";
  if (on) {
    const pv = $("pvVideo");
    if (pv && state.selected) {
      pv.pause();
      // Tiempo de sección -> tiempo del video (resta el offset: con
      // clip-video el tiempo 0 es el inicio del clip).
      try { pv.currentTime = Math.max(0, state.selected.start + state.coverAt - (state.pvOffset || 0) + 0.01); } catch (e) { /* aún sin metadatos */ }
    }
    updateCoverTitle();
  }
}
$("pvCoverBtn").onclick = () => {
  const phone = $("phone");
  setCoverMode(!(phone && phone.classList.contains("cover-mode")));
};
$("hookTitle").addEventListener("input", () => {
  updateCoverTitle();
  setCoverMode(true);
});

// ---------- Modal Configurar portada (2026-11-11, petición de Juan) ----------
// Un popup SOLO para la portada, antes de recortar el video: vista
// previa 9:16 en vivo (fotograma + título, sin marco ni marca ni
// subtítulos), selector del fotograma, texto editable, color del texto,
// borde de la letra (color + grosor), fondo de las letras (color + si
// lleva caja) y tamaño. "Aplicar portada" guarda todo en el paso 3 y en
// el render; "Cancelar" no cambia nada.
const COVER_SWATCHES = ["#FFFFFF", "#FADE4C", "#E10600", "#000000", "#00E676", "#00B0FF", "#FF6D00", "#B388FF"];
function buildSwatches(elId, getVal, setVal) {
  const box = $(elId);
  if (!box) return;
  box.innerHTML = "";
  COVER_SWATCHES.forEach((c) => {
    const b = document.createElement("button");
    b.type = "button";
    b.style.background = c;
    b.title = c;
    b.onclick = () => { setVal(c); updateCoverModalPreview(); };
    box.appendChild(b);
  });
  const mark = () => {
    [...box.children].forEach((ch) => ch.classList.toggle("sel", ch.title.toUpperCase() === (getVal() || "").toUpperCase()));
  };
  box._mark = mark;
  mark();
}
function wrapTitle(text) {
  // Igual que el backend (textwrap width=18): envuelve el título en
  // líneas cortas para que se lea grande.
  const words = (text || "").trim().split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > 18 && cur) { lines.push(cur); cur = w; }
    else cur = (cur + " " + w).trim();
  }
  if (cur) lines.push(cur);
  return lines.join("\n") || "Título de enganche";
}
function updateCoverModalPreview() {
  const st = state.coverStyle;
  const titleEl = $("coverModalTitle2");
  if (titleEl) {
    titleEl.textContent = wrapTitle($("coverModalTitleInput").value || coverTitleText());
    titleEl.style.color = st.color;
    titleEl.style.fontSize = Math.round(st.size * 0.22) + "px";
    titleEl.style.textShadow = st.borderW > 0
      ? `0 0 ${st.borderW}px ${st.borderColor}, 0 ${Math.max(1, Math.round(st.borderW / 2))}px 0 ${st.borderColor}, 0 0 ${st.borderW * 2}px ${st.borderColor}`
      : "none";
    titleEl.style.background = st.bg ? "rgba(0,0,0,0.55)" : "transparent";
    if (st.bg) titleEl.style.background = hexToRgba(st.bgColor, 0.55);
    // Posición del texto (igual que el backend: la esquina del bloque
    // queda a posX/posY del espacio disponible; 0 = arriba/izquierda,
    // 0.5 = centrado, 1 = abajo/derecha).
    titleEl.style.left = (st.posX * 100) + "%";
    titleEl.style.right = "auto";
    titleEl.style.top = (st.posY * 100) + "%";
    titleEl.style.transform = `translate(${-st.posX * 100}%, ${-st.posY * 100}%)`;
  }
  const v = $("coverVideo");
  if (v && state.selected) {
    // Tiempo de sección -> tiempo del video (resta el offset).
    try { v.currentTime = Math.max(0, state.selected.start + state.coverAt - (state.pvOffset || 0) + 0.01); } catch (e) { /* aún sin metadatos */ }
  }
  // Sincroniza los controles con el estado.
  const sync = (id, val) => { const el = $(id); if (el && el.value !== String(val)) el.value = val; };
  sync("coverColor", st.color); sync("coverColorHex", st.color);
  sync("coverBorderColor", st.borderColor); sync("coverBorderHex", st.borderColor);
  sync("coverBgColor", st.bgColor); sync("coverBgHex", st.bgColor);
  sync("coverBorderW", st.borderW); sync("coverSize", st.size);
  sync("coverPosX", Math.round(st.posX * 100)); sync("coverPosY", Math.round(st.posY * 100));
  const bw = $("coverBorderWVal"); if (bw) bw.textContent = st.borderW + " px";
  const sv = $("coverSizeVal"); if (sv) sv.textContent = st.size;
  const posName = (v) => v < 0.35 ? "Arriba" : v > 0.65 ? "Abajo" : "Centro";
  const posNameX = (v) => v < 0.35 ? "Izquierda" : v > 0.65 ? "Derecha" : "Centro";
  const pyv = $("coverPosYVal"); if (pyv) pyv.textContent = posName(st.posY);
  const pxv = $("coverPosXVal"); if (pxv) pxv.textContent = posNameX(st.posX);
  const bgOn = $("coverBgOn"); if (bgOn) bgOn.checked = !!st.bg;
  ["coverColorSwatches", "coverBorderSwatches", "coverBgSwatches"].forEach((id) => {
    const box = $(id);
    if (box && box._mark) box._mark();
  });
}
function hexToRgba(hex, alpha) {
  const h = (hex || "#000000").replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  if (isNaN(n)) return `rgba(0,0,0,${alpha})`;
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}
function openCoverModal() {
  if (!state.selected || !state.clipsJob) { alert("Primero elige un clip para configurar su portada."); return; }
  const m = $("coverModal");
  // Carga el estado actual en los controles del modal.
  $("coverModalTitleInput").value = $("hookTitle").value.trim() || (state.selected.title || "");
  const clipDur = Math.max(0, (state.selected.end - state.selected.start) || 0);
  const rng = $("coverModalRange");
  rng.max = Math.max(clipDur, 0.1);
  rng.value = state.coverAt;
  $("coverModalVal").textContent = state.coverAt.toFixed(1) + " s";
  const cv = $("coverVideo");
  // El video del modal es el trozo del clip (clip-video): la fuente del
  // análisis es solo audio, así que /source ya no tiene video.
  const src = `/api/jobs/${state.clipsJob.id}/clip-video?start=${state.selected.abs_start}&end=${state.selected.abs_end}`;
  if (cv.dataset.src !== src) { cv.dataset.src = src; cv.src = src; cv.load(); }
  m.hidden = false;
  updateCoverModalPreview();
  try { cv.currentTime = Math.max(0, state.selected.start + state.coverAt - (state.pvOffset || 0) + 0.01); } catch (e) {}
}
function closeCoverModal() { $("coverModal").hidden = true; }
$("btnCoverModal").onclick = openCoverModal;
$("coverModalClose").onclick = closeCoverModal;
$("coverModalCancel").onclick = closeCoverModal;
$("coverModal").addEventListener("click", (e) => { if (e.target === $("coverModal")) closeCoverModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("coverModal").hidden) closeCoverModal();
});
$("coverModalRange").addEventListener("input", () => {
  const v = parseFloat($("coverModalRange").value || "0");
  state.coverAt = v;
  $("coverModalVal").textContent = v.toFixed(1) + " s";
  updateCoverModalPreview();
});
$("coverModalTitleInput").addEventListener("input", updateCoverModalPreview);
$("coverColor").addEventListener("input", () => { state.coverStyle.color = $("coverColor").value; updateCoverModalPreview(); });
$("coverColorHex").addEventListener("input", () => { state.coverStyle.color = $("coverColorHex").value; updateCoverModalPreview(); });
$("coverBorderColor").addEventListener("input", () => { state.coverStyle.borderColor = $("coverBorderColor").value; updateCoverModalPreview(); });
$("coverBorderHex").addEventListener("input", () => { state.coverStyle.borderColor = $("coverBorderHex").value; updateCoverModalPreview(); });
$("coverBorderW").addEventListener("input", () => { state.coverStyle.borderW = parseInt($("coverBorderW").value || "0", 10); updateCoverModalPreview(); });
$("coverBgColor").addEventListener("input", () => { state.coverStyle.bgColor = $("coverBgColor").value; updateCoverModalPreview(); });
$("coverBgHex").addEventListener("input", () => { state.coverStyle.bgColor = $("coverBgHex").value; updateCoverModalPreview(); });
$("coverBgOn").addEventListener("change", () => { state.coverStyle.bg = $("coverBgOn").checked; updateCoverModalPreview(); });
$("coverSize").addEventListener("input", () => { state.coverStyle.size = parseInt($("coverSize").value || "96", 10); updateCoverModalPreview(); });
$("coverPosY").addEventListener("input", () => { state.coverStyle.posY = parseInt($("coverPosY").value || "50", 10) / 100; updateCoverModalPreview(); });
$("coverPosX").addEventListener("input", () => { state.coverStyle.posX = parseInt($("coverPosX").value || "50", 10) / 100; updateCoverModalPreview(); });
buildSwatches("coverColorSwatches", () => state.coverStyle.color, (c) => { state.coverStyle.color = c; });
buildSwatches("coverBorderSwatches", () => state.coverStyle.borderColor, (c) => { state.coverStyle.borderColor = c; });
buildSwatches("coverBgSwatches", () => state.coverStyle.bgColor, (c) => { state.coverStyle.bgColor = c; });
$("coverModalApply").onclick = () => {
  // Aplica la portada al paso 3: título + fotograma + estilo. El render
  // (Render / Guardar título y portada) ya los toma de buildRenderBody.
  const title = $("coverModalTitleInput").value.trim();
  $("hookTitle").value = title;
  const cov = $("coverRange");
  cov.max = $("coverModalRange").max;
  cov.value = state.coverAt;
  $("coverVal").textContent = state.coverAt.toFixed(1) + " s";
  updateCoverTitle();
  setCoverMode(true);
  closeCoverModal();
};
$("pvVideo").addEventListener("timeupdate", () => {
  const pv = $("pvVideo");
  if (!state.pvClip) return;
  const dur = state.pvClip.end - state.pvClip.start;
  // t es relativo al inicio del CLIP (así están state.words): tiempo del
  // video + pvOffset (inicio del clip en la sección) - inicio del clip.
  const t = pv.currentTime + (state.pvOffset || 0) - state.pvClip.start;
  if (t >= dur) { // fin del clip: vuelve al inicio (bucle del trozo)
    try { pv.currentTime = Math.max(0, state.pvClip.start - (state.pvOffset || 0) + 0.01); } catch (e) {}
    return;
  }
  if (t >= 0) { updatePreviewSubs(t); applyCamTrack(t); }
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
  // Al abrir Editar se muestra la portada (fotograma + texto gancho,
  // sin subtítulos) para ver cómo queda antes de guardar.
  updateCoverTitle();
  setCoverMode(true);
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
  // Al mover la portada se muestra la vista previa de portada (solo
  // fotograma + texto gancho, sin subtítulos) para ver cómo queda.
  setCoverMode(true);
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
  dual: "Para entrevistas con dos personas sentadas a izquierda y derecha: la mitad izquierda arriba y la derecha abajo, cada una centrada.",
  split: "Para videos con una cara en una esquina (reacción, entrevista, juego): la cara arriba y el contenido abajo, en dos pantallas.",
};
// Mini diagrama del modo de cámara (2026-11-11, Juan: los textos
// decían "el juego abajo" y no todo video es un juego; además pidió
// un icono al lado para ver cómo queda). El dibujo cambia con el modo.
const CAMERA_PREVIEWS = {
  center: `<svg viewBox="0 0 56 100" width="56" height="100"><rect width="56" height="100" fill="#0A0A1F"/><circle cx="28" cy="38" r="10" fill="#e4e4e7"/><path d="M12 78 Q28 58 44 78 L44 100 L12 100 Z" fill="#e4e4e7"/></svg>`,
  follow_speaker: `<svg viewBox="0 0 56 100" width="56" height="100"><rect width="56" height="100" fill="#0A0A1F"/><circle cx="28" cy="34" r="9" fill="#e4e4e7"/><path d="M14 70 Q28 54 42 70 L42 88 L14 88 Z" fill="#e4e4e7"/><path d="M6 94 h12 m-3 -3 l3 3 -3 3 M50 94 h-12 m3 -3 l-3 3 3 3" stroke="#A855F7" stroke-width="2" fill="none"/></svg>`,
  follow_person: `<svg viewBox="0 0 56 100" width="56" height="100"><rect width="56" height="100" fill="#0A0A1F"/><circle cx="28" cy="38" r="12" fill="none" stroke="#A855F7" stroke-width="2" stroke-dasharray="4 3"/><circle cx="28" cy="38" r="9" fill="#e4e4e7"/><path d="M13 76 Q28 58 43 76 L43 100 L13 100 Z" fill="#e4e4e7"/></svg>`,
  dual: `<svg viewBox="0 0 56 100" width="56" height="100"><rect width="56" height="100" fill="#0A0A1F"/><circle cx="28" cy="16" r="7" fill="#e4e4e7"/><path d="M16 38 Q28 26 40 38 L40 50 L16 50 Z" fill="#e4e4e7"/><line x1="0" y1="50" x2="56" y2="50" stroke="#A855F7" stroke-width="2"/><circle cx="28" cy="66" r="7" fill="#FADE4C"/><path d="M16 88 Q28 76 40 88 L40 100 L16 100 Z" fill="#FADE4C"/></svg>`,
  split: `<svg viewBox="0 0 56 100" width="56" height="100"><rect width="56" height="100" fill="#0A0A1F"/><rect x="6" y="6" width="44" height="34" rx="4" fill="#27272a" stroke="#A855F7" stroke-width="1.5"/><circle cx="28" cy="18" r="6" fill="#e4e4e7"/><path d="M18 36 Q28 28 38 36 L38 40 L18 40 Z" fill="#e4e4e7"/><rect x="6" y="46" width="44" height="48" rx="4" fill="#1f1f23"/><path d="M24 62 L38 70 L24 78 Z" fill="#A855F7"/></svg>`,
};
function updateCameraPreview() {
  const box = $("cameraPreview");
  if (!box) return;
  const m = ($("cameraMode") && $("cameraMode").value) || "center";
  box.innerHTML = CAMERA_PREVIEWS[m] || CAMERA_PREVIEWS.center;
}
// Dibuja el mini diagrama al cargar (el select arranca en
// "Seguir al que habla").
updateCameraPreview();
$("cameraMode").onchange = () => {
  const m = $("cameraMode").value;
  $("cameraPersonRow").hidden = m !== "follow_person";
  $("facecamRow").hidden = m !== "split";
  $("cameraHint").textContent = CAMERA_HINTS[m] || CAMERA_HINTS.center;
  updateCameraPreview();
  // Cambiar el modo mueve el encuadre de la vista previa al instante
  // (si hay un clip elegido).
  if (state.selected) loadCamTrack();
};
if ($("cameraPerson")) {
  $("cameraPerson").onchange = () => { if (state.selected) loadCamTrack(); };
}

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
    // Estilo de la portada (modal Configurar portada): color del texto,
    // borde de la letra, fondo de las letras y tamaño del título.
    title_color: state.coverStyle.color,
    title_border_color: state.coverStyle.borderColor,
    title_border_w: state.coverStyle.borderW,
    title_bg_color: state.coverStyle.bgColor,
    title_bg: state.coverStyle.bg,
    title_size: state.coverStyle.size,
    title_pos_x: state.coverStyle.posX,
    title_pos_y: state.coverStyle.posY,
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
      // Mensaje en vivo del servidor cuando lo hay (descarga, etc.).
      if ((j.progress || 0) >= 70) renderSteps(3, 3, (j.message && String(j.message).trim()) || FLOW_MSGS[3]);
      else if (j.message && String(j.message).trim()) renderSteps(3, 3, String(j.message).trim());
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
