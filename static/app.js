const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = JSON.parse(sessionStorage.getItem("bookingDraft") || "{}");
state.services = Array.isArray(state.services) ? state.services : [];
state.mode = state.mode || "first";

let business = { settings: {}, services: [] };
let verified = false;
let isNewCustomer = true;
let currentStep = 1;

const statusLabels = {
  pending: "ממתין לאישור",
  approved: "מאושר",
  rejected: "נדחה",
  cancelled: "בוטל",
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function saveDraft() {
  sessionStorage.setItem("bookingDraft", JSON.stringify(state));
}

function localIso(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseDate(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day, 12);
}

function formatDate(value, includeYear = false) {
  return new Intl.DateTimeFormat("he-IL", {
    weekday: "long",
    day: "numeric",
    month: "long",
    ...(includeYear ? { year: "numeric" } : {}),
  }).format(parseDate(value));
}

function formatMoney(value) {
  return `₪${new Intl.NumberFormat("he-IL").format(Number(value) || 0)}`;
}

function errorMessage(error) {
  if (typeof error?.message === "string") return error.message;
  return "לא הצלחנו להשלים את הפעולה. בדקי את החיבור ונסי שוב.";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "content-type": "application/json", ...(options.headers || {}) } : options.headers,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join(". ")
      : payload.detail;
    const error = new Error(detail || "הפעולה לא הושלמה");
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function setBusy(button, busy, busyLabel = "רק רגע...") {
  const label = $("span", button);
  if (!button.dataset.label) button.dataset.label = (label || button).textContent.trim();
  button.disabled = busy;
  button.classList.toggle("is-loading", busy);
  (label || button).textContent = busy ? busyLabel : button.dataset.label;
}

function toast(message, tone = "success") {
  const region = $("#toastRegion");
  const item = create("div", `toast toast--${tone}`);
  item.append(icon(tone === "error" ? "circle-alert" : "check"), create("span", "", message));
  region.replaceChildren(item);
  window.setTimeout(() => item.remove(), 4200);
}

function selectedTotals() {
  const services = business.services.filter((service) => state.services.includes(service.id));
  return {
    services,
    duration: services.reduce((sum, service) => sum + service.duration_minutes, 0),
    price: services.reduce((sum, service) => sum + service.price, 0),
  };
}

function updateSummary() {
  const totals = selectedTotals();
  const summaryBar = $("#summaryBar");
  summaryBar.hidden = !totals.duration || currentStep !== 1;
  $("#total").textContent = totals.duration
    ? `${totals.services.length} ${totals.services.length === 1 ? "שירות" : "שירותים"} · ${totals.duration} דק׳ · ${formatMoney(totals.price)}`
    : "בחרי שירות";
}

function setStep(step) {
  currentStep = step;
  const content = {
    1: ["מה תרצי לקבוע?", "אפשר לבחור שירות אחד או כמה שירותים."],
    2: ["מתי נוח לך?", "בחרי את המועד שמתאים לך מתוך השעות הפנויות."],
    3: ["כמעט סיימנו", "נשאר לאמת את הטלפון ולאשר את הפרטים."],
  };
  $("#flowTitle").textContent = content[step][0];
  $("#flowHint").textContent = content[step][1];
  $("#stepNumber").textContent = step;
  $("#stepCounterValue").textContent = step;
  $("#serviceStep").hidden = step !== 1;
  $("#timeStep").hidden = step !== 2;
  $("#confirmStep").hidden = step !== 3;
  $$('[data-step-indicator]').forEach((item) => {
    const itemStep = Number(item.dataset.stepIndicator);
    item.classList.toggle("active", itemStep === step);
    item.classList.toggle("complete", itemStep < step);
    if (itemStep === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
  updateSummary();
  if (step > 1) $(".booking-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderBusiness() {
  const settings = business.settings;
  $("#brand").textContent = settings.name || "מערכת תורים";
  $("#bizName").textContent = settings.name || "קביעת תור";
  $("#bizDesc").textContent = settings.description || "בחירת שירות ומועד בכמה צעדים פשוטים";
  document.title = `קביעת תור · ${settings.name || "מערכת תורים"}`;
  if (settings.cover_image) $("#cover").style.backgroundImage = `url("${settings.cover_image}")`;
  if (settings.profile_image) $("#profile").style.backgroundImage = `url("${settings.profile_image}")`;

  const links = $("#links");
  links.replaceChildren();
  const phone = settings.phone ? String(settings.phone) : "";
  const items = [
    ["טלפון", phone && `tel:${phone}`, "phone"],
    ["WhatsApp", phone && `https://wa.me/${phone.replace(/\D/g, "")}`, "message"],
    ["Waze", settings.waze_url, "navigation"],
    ["רשת חברתית", settings.social_url, "instagram"],
  ];
  items.forEach(([label, href, iconName]) => {
    if (!href) return;
    const link = create("a", "contact-btn");
    link.href = href;
    link.setAttribute("aria-label", label);
    if (href.startsWith("http")) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    link.append(icon(iconName), create("span", "", label));
    links.append(link);
  });
}

function renderServices() {
  const box = $("#services");
  box.replaceChildren();
  business.services.forEach((service) => {
    const selected = state.services.includes(service.id);
    const button = create("button", "service-card");
    button.type = "button";
    button.setAttribute("aria-pressed", String(selected));
    const check = create("span", "service-card__check");
    check.append(icon("check"));
    const content = create("span", "service-card__content");
    content.append(create("strong", "", service.name));
    if (service.category) content.append(create("small", "", service.category));
    const meta = create("span", "service-card__meta");
    meta.append(create("span", "ltr", `${service.duration_minutes} דק׳`), create("b", "ltr", formatMoney(service.price)));
    button.append(check, content, meta);
    button.addEventListener("click", () => {
      state.services = selected
        ? state.services.filter((id) => id !== service.id)
        : [...state.services, service.id];
      delete state.time;
      saveDraft();
      renderServices();
    });
    box.append(button);
  });
  if (!business.services.length) {
    box.append(emptyState("לא נמצאו שירותים זמינים כרגע", "אפשר ליצור קשר עם העסק לפרטים נוספים."));
  }
  updateSummary();
}

function emptyState(title, description) {
  const box = create("div", "empty-state");
  const mark = create("div", "empty-state__icon");
  mark.append(icon("calendar-off"));
  box.append(mark, create("strong", "", title), create("p", "", description));
  return box;
}

function renderSlotSkeletons() {
  const box = $("#slots");
  box.replaceChildren();
  const group = create("div", "slot-day");
  group.append(create("div", "skeleton skeleton--label"));
  const grid = create("div", "slot-grid");
  for (let index = 0; index < 8; index += 1) grid.append(create("div", "skeleton skeleton--slot"));
  group.append(grid);
  box.append(group);
}

async function loadSlots() {
  const totals = selectedTotals();
  if (!totals.duration) return;
  const start = state.mode === "day" ? $("#dayPick").value : localIso();
  if (!start) return;
  const endDate = parseDate(start);
  endDate.setDate(endDate.getDate() + (state.mode === "day" ? 0 : 21));
  const box = $("#slots");
  box.setAttribute("aria-busy", "true");
  renderSlotSkeletons();
  try {
    const data = await api(`/api/slots?date_from=${start}&date_to=${localIso(endDate)}&duration=${totals.duration}`);
    box.replaceChildren();
    let remaining = state.mode === "day" ? Number.POSITIVE_INFINITY : 16;
    data.days.forEach((day) => {
      const times = day.times.slice(0, remaining);
      if (!times.length) return;
      remaining -= times.length;
      const section = create("section", "slot-day");
      section.append(create("h3", "slot-day__title", formatDate(day.date)));
      const grid = create("div", "slot-grid");
      times.forEach((time, index) => {
        const button = create("button", "time-chip", time);
        button.type = "button";
        button.style.setProperty("--delay", `${index * 15}ms`);
        button.setAttribute("aria-label", `${formatDate(day.date, true)}, בשעה ${time}`);
        button.setAttribute("aria-pressed", String(state.date === day.date && state.time === time));
        button.addEventListener("click", () => {
          state.date = day.date;
          state.time = time;
          saveDraft();
          showConfirmation();
        });
        grid.append(button);
      });
      section.append(grid);
      box.append(section);
    });
    if (!box.childElementCount) {
      box.append(emptyState(
        state.mode === "day" ? "אין שעות פנויות ביום הזה" : "אין שעות פנויות בשלושת השבועות הקרובים",
        state.mode === "day" ? "בחרי תאריך אחר ונבדוק שוב." : "אפשר ליצור קשר עם העסק ולבדוק אפשרויות נוספות."
      ));
    }
  } catch (error) {
    box.replaceChildren();
    const banner = create("div", "banner banner--error");
    banner.append(icon("circle-alert"), create("span", "", errorMessage(error)));
    box.append(banner);
  } finally {
    box.setAttribute("aria-busy", "false");
  }
}

function showConfirmation() {
  const totals = selectedTotals();
  const summary = $("#chosen");
  summary.replaceChildren();
  const calendar = create("div", "appointment-summary__icon");
  calendar.append(icon("calendar-check"));
  const content = create("div", "appointment-summary__content");
  content.append(
    create("span", "", totals.services.map((service) => service.name).join(", ")),
    create("strong", "", `${formatDate(state.date, true)} · ${state.time}`),
    create("small", "", `${totals.duration} דק׳ · ${formatMoney(totals.price)}`)
  );
  summary.append(calendar, content);
  if (business.settings.preparation_message) {
    summary.append(create("p", "appointment-summary__note", business.settings.preparation_message));
  }
  setStep(3);
}

function markVerified(email) {
  verified = true;
  $("#authBox").classList.add("is-verified");
  $("#authStatus").textContent = `המייל ${email || "שלך"} אומת בהצלחה.`;
  $("#nameField").hidden = !isNewCustomer;
  if (isNewCustomer) $("#name").focus();
}

async function requestCode() {
  const button = $("#sendCode");
  const email = $("#email").value.trim();
  if (!email) {
    $("#authStatus").textContent = "הזיני כתובת מייל כדי לקבל קוד.";
    $("#email").focus();
    return;
  }
  setBusy(button, true, "שולחת...");
  try {
    await api("/api/auth/request-code", { method: "POST", body: JSON.stringify({ email }) });
    $("#codeField").hidden = false;
    $("#authStatus").textContent = "הקוד נשלח. הוא תקף למשך 5 דקות.";
    $("#code").focus();
  } catch (error) {
    $("#authStatus").textContent = errorMessage(error);
  } finally {
    setBusy(button, false);
  }
}

async function verifyCode() {
  const button = $("#verify");
  setBusy(button, true, "בודקת...");
  try {
    const result = await api("/api/auth/verify", {
      method: "POST",
      body: JSON.stringify({ email: $("#email").value, code: $("#code").value }),
    });
    isNewCustomer = result.is_new;
    markVerified($("#email").value);
  } catch (error) {
    $("#authStatus").textContent = errorMessage(error);
    $("#code").select();
  } finally {
    setBusy(button, false);
  }
}

async function createBooking() {
  if (!verified) {
    $("#authStatus").textContent = "צריך לאמת את כתובת המייל לפני קביעת התור.";
    $("#email").focus();
    return;
  }
  if (isNewCustomer && $("#name").value.trim().length < 2) {
    toast("הזיני שם מלא כדי שנדע למי התור.", "error");
    $("#name").focus();
    return;
  }
  const button = $("#book");
  setBusy(button, true, "קובעת את התור...");
  try {
    await api("/api/bookings", {
      method: "POST",
      body: JSON.stringify({
        service_ids: state.services,
        date: state.date,
        time: state.time,
        name: $("#name").value,
        notes: $("#notes").value,
      }),
    });
    sessionStorage.removeItem("bookingDraft");
    $("#successModal").showModal();
  } catch (error) {
    toast(errorMessage(error), "error");
    if (error.status === 409) {
      delete state.time;
      saveDraft();
      setStep(2);
      loadSlots();
    }
  } finally {
    setBusy(button, false);
  }
}

function bookingCard(booking) {
  const services = JSON.parse(booking.services_snapshot || "[]");
  const card = create("article", "booking-card");
  const header = create("div", "booking-card__header");
  const date = create("div", "booking-card__date");
  date.append(create("strong", "", formatDate(booking.booking_date)), create("span", "ltr", booking.booking_time));
  const badge = create("span", `status-badge status-badge--${booking.status}`, statusLabels[booking.status] || booking.status);
  header.append(date, badge);
  const details = create("p", "booking-card__services", services.map((service) => service.name).join(", "));
  const meta = create("p", "booking-card__meta", `${booking.duration_minutes} דק׳ · ${formatMoney(booking.price)}`);
  const actions = create("div", "booking-card__actions");
  const calendar = create("a", "btn btn--secondary btn--compact");
  calendar.href = `/api/bookings/${booking.id}/ics`;
  calendar.append(icon("calendar-days"), create("span", "", "הוספה ליומן"));
  actions.append(calendar);
  if (["pending", "approved"].includes(booking.status)) {
    const cancel = create("button", "btn btn--ghost btn--compact", "ביטול תור");
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      if (!(await confirmAction())) return;
      try {
        await api(`/api/bookings/${booking.id}/cancel`, { method: "POST" });
        toast("התור בוטל.");
        loadMyBookings();
      } catch (error) {
        toast(errorMessage(error), "error");
      }
    });
    actions.append(cancel);
  }
  if (["cancelled", "rejected"].includes(booking.status)) {
    const hide = create("button", "btn btn--ghost btn--compact", "הסתרה מהרשימה");
    hide.type = "button";
    hide.addEventListener("click", async () => {
      await api(`/api/bookings/${booking.id}/hide`, { method: "POST" });
      loadMyBookings();
    });
    actions.append(hide);
  }
  if (booking.arrival_status === "requested") {
    const arrival = create("div", "arrival-request");
    arrival.append(create("p", "", "בעלת העסק מבקשת לוודא שתגיעי לתור."));
    [["אגיע", "confirmed"], ["לא אוכל להגיע", "declined"]].forEach(([label, answer]) => {
      const button = create("button", answer === "confirmed" ? "btn btn--primary btn--compact" : "btn btn--secondary btn--compact", label);
      button.type = "button";
      button.addEventListener("click", async () => {
        await api(`/api/bookings/${booking.id}/arrival`, { method: "POST", body: JSON.stringify({ answer }) });
        toast("התשובה נשמרה.");
        loadMyBookings();
      });
      arrival.append(button);
    });
    card.append(arrival);
  }
  card.prepend(header, details, meta, actions);
  return card;
}

async function loadMyBookings() {
  const modal = $("#mineModal");
  const list = $("#mineList");
  list.replaceChildren(create("div", "skeleton booking-skeleton"), create("div", "skeleton booking-skeleton"));
  if (!modal.open) modal.showModal();
  try {
    const data = await api("/api/bookings/mine");
    list.replaceChildren();
    if (!data.bookings.length) {
      list.append(emptyState("עדיין אין לך תורים", "לאחר קביעת תור הוא יופיע כאן עם כל הפרטים."));
      return;
    }
    data.bookings.forEach((booking) => list.append(bookingCard(booking)));
  } catch (error) {
    modal.close();
    toast("כדי לראות את התורים שלך, אמתִי קודם את הטלפון בזמן קביעת תור.", "error");
  }
}

function confirmAction() {
  const dialog = $("#confirmDialog");
  dialog.showModal();
  return new Promise((resolve) => {
    $("#confirmAccept").onclick = () => { dialog.close(); resolve(true); };
    $("#confirmCancel").onclick = () => { dialog.close(); resolve(false); };
    dialog.oncancel = () => resolve(false);
  });
}

async function boot() {
  try {
    business = await api("/api/business");
    state.services = state.services.filter((id) => business.services.some((service) => service.id === id));
    state.date = state.date || localIso();
    $("#dayPick").value = state.date;
    $("#dayPick").min = localIso();
    const lastDay = new Date();
    lastDay.setDate(lastDay.getDate() + Number(business.settings.max_days_ahead || 60));
    $("#dayPick").max = localIso(lastDay);
    $$('[data-mode]').forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.mode === state.mode)));
    $("#dateField").hidden = state.mode !== "day";
    renderBusiness();
    renderServices();
  } catch (error) {
    $("#services").replaceChildren();
    const banner = create("div", "banner banner--error");
    banner.append(icon("circle-alert"), create("span", "", errorMessage(error)));
    $("#services").append(banner);
  }
  try {
    const me = await api("/api/me");
    verified = true;
    isNewCustomer = !me.name;
    markVerified(me.email);
  } catch {
    verified = false;
  }
}

$("#nextTime").addEventListener("click", () => {
  if (!selectedTotals().duration) {
    toast("בחרי לפחות שירות אחד כדי להמשיך.", "error");
    return;
  }
  setStep(2);
  loadSlots();
});

$$('[data-mode]').forEach((button) => button.addEventListener("click", () => {
  state.mode = button.dataset.mode;
  saveDraft();
  $$('[data-mode]').forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  $("#dateField").hidden = state.mode !== "day";
  loadSlots();
}));

$("#dayPick").addEventListener("change", () => {
  state.date = $("#dayPick").value;
  delete state.time;
  saveDraft();
  loadSlots();
});

$('[data-back="services"]').addEventListener("click", () => setStep(1));
$('[data-back="time"]').addEventListener("click", () => { setStep(2); loadSlots(); });
$("#sendCode").addEventListener("click", requestCode);
$("#verify").addEventListener("click", verifyCode);
$("#book").addEventListener("click", createBooking);
$("#mineBtn").addEventListener("click", loadMyBookings);
$('[data-close]').addEventListener("click", () => $("#mineModal").close());
$("#successDone").addEventListener("click", () => window.location.reload());

boot();
