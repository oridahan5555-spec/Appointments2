const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

let activeTab = "bookings";

const tabMeta = {
  bookings: ["תורים", "כל התורים הקרובים, האישורים והעדכונים במקום אחד."],
  services: ["שירותים", "ניהול השירותים, המחירים ומשך כל טיפול."],
  hours: ["שעות פעילות", "השעות הקבועות שבהן אפשר לקבוע תורים."],
  overrides: ["ימים מיוחדים", "שעות שונות או ימי חופש בתאריכים מסוימים."],
  blocks: ["חסימות", "שמירת זמן ביומן שבו לא ניתן לקבוע תור."],
  customers: ["לקוחות", "פרטי לקוחות, הערות פנימיות והגבלות."],
  settings: ["הגדרות העסק", "הפרטים שמופיעים ללקוחות והגדרות ההזמנה."],
};

const statusLabels = {
  pending: "ממתין לאישור",
  approved: "מאושר",
  rejected: "נדחה",
  cancelled: "בוטל",
};

const dayNames = ["יום ראשון", "יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "שבת"];

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

function setBusy(button, busy, busyLabel = "שומרת...") {
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

function button(label, handler, options = {}) {
  const node = create("button", `btn ${options.className || "btn--secondary"}`);
  node.type = "button";
  if (options.icon) node.append(icon(options.icon));
  node.append(create("span", "", label));
  node.addEventListener("click", handler);
  return node;
}

function field(name, label, value = "", options = {}) {
  const wrapper = create("label", `field ${options.className || ""}`.trim());
  const labelText = create("span", "field__label", label);
  if (options.hint) labelText.append(create("small", "", options.hint));
  let control;
  if (options.multiline) {
    control = create("textarea", "input");
    control.rows = options.rows || 3;
  } else if (options.select) {
    control = create("select", "input");
    options.select.forEach(([optionValue, optionLabel]) => {
      const option = create("option", "", optionLabel);
      option.value = optionValue;
      control.append(option);
    });
  } else {
    control = create("input", "input");
    control.type = options.type || "text";
  }
  control.name = name;
  control.value = value ?? "";
  if (options.placeholder) control.placeholder = options.placeholder;
  if (options.min !== undefined) control.min = options.min;
  if (options.max !== undefined) control.max = options.max;
  if (options.step !== undefined) control.step = options.step;
  if (options.required) control.required = true;
  if (options.dir) control.dir = options.dir;
  wrapper.append(labelText, control);
  return wrapper;
}

function switchField(name, label, checked = false, description = "") {
  const wrapper = create("label", "switch-field");
  const copy = create("span", "switch-field__copy");
  copy.append(create("strong", "", label));
  if (description) copy.append(create("small", "", description));
  const control = create("span", "switch");
  const input = create("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = Boolean(Number(checked));
  control.append(input, create("span", "switch__track"));
  wrapper.append(copy, control);
  return wrapper;
}

function value(root, name) {
  return $(`[name="${name}"]`, root)?.value ?? "";
}

function checked(root, name) {
  return $(`[name="${name}"]`, root)?.checked ? 1 : 0;
}

function sectionHeader(title, description, action) {
  const header = create("header", "admin-section__header");
  const copy = create("div");
  copy.append(create("h2", "", title), create("p", "", description));
  header.append(copy);
  if (action) header.append(action);
  return header;
}

function emptyState(title, description) {
  const box = create("div", "empty-state");
  const mark = create("div", "empty-state__icon");
  mark.append(icon("calendar-off"));
  box.append(mark, create("strong", "", title), create("p", "", description));
  return box;
}

function errorBanner(message) {
  const banner = create("div", "banner banner--error");
  banner.append(icon("circle-alert"), create("span", "", message));
  return banner;
}

function showLoading() {
  const view = $("#view");
  view.setAttribute("aria-busy", "true");
  view.replaceChildren();
  const skeleton = create("div", "admin-skeleton");
  skeleton.append(create("div", "skeleton skeleton--heading"));
  for (let index = 0; index < 3; index += 1) skeleton.append(create("div", "skeleton skeleton--admin-row"));
  view.append(skeleton);
}

function showAdmin() {
  document.body.classList.add("is-authenticated");
  $(".login-card").hidden = true;
  $("#admin").hidden = false;
  $("#ownerNav").hidden = false;
  $("#ownerSidebarFooter").hidden = false;
  switchTab(activeTab);
}

function switchTab(tab) {
  activeTab = tab;
  const [title, description] = tabMeta[tab];
  $("#pageTitle").textContent = title;
  $("#pageDescription").textContent = description;
  $$('[data-tab]').forEach((item) => item.setAttribute("aria-selected", String(item.dataset.tab === tab)));
  const activeItem = $(`[data-tab="${tab}"]`);
  if (window.matchMedia("(max-width: 900px)").matches) {
    activeItem.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }
  showLoading();
  loadTab();
}

async function loadTab() {
  try {
    await {
      bookings: renderBookings,
      services: renderServices,
      hours: renderHours,
      overrides: renderOverrides,
      blocks: renderBlocks,
      customers: renderCustomers,
      settings: renderSettings,
    }[activeTab]();
  } catch (error) {
    $("#stats").replaceChildren();
    $("#view").replaceChildren(errorBanner(errorMessage(error)));
  } finally {
    $("#view").setAttribute("aria-busy", "false");
  }
}

function statCard(label, number, iconName, tone = "accent") {
  const card = create("article", `stat-card stat-card--${tone}`);
  const mark = create("div", "stat-card__icon");
  mark.append(icon(iconName));
  const copy = create("div");
  copy.append(create("strong", "", String(number)), create("span", "", label));
  card.append(mark, copy);
  return card;
}

async function updateBookingStatus(booking, status, actionButton) {
  const messages = {
    approved: "לאשר את התור? אם Google Calendar מחובר, האירוע ייכנס ליומן עם תזכורת 15 דקות לפני.",
    rejected: "לדחות את התור? הלקוחה תקבל הודעה במייל.",
    cancelled: "לבטל את התור? המועד יחזור להיות פנוי.",
  };
  if (!(await confirmAction(messages[status], status === "approved" ? "אישור תור" : "אישור פעולה", status === "approved" ? "אישור" : "המשך"))) return;
  setBusy(actionButton, true, "מעדכנת...");
  try {
    await api(`/api/owner/bookings/${booking.id}/status`, { method: "POST", body: JSON.stringify({ status }) });
    toast(status === "approved" ? "התור אושר והיומן עודכן." : "סטטוס התור עודכן.");
    await renderBookings();
  } catch (error) {
    toast(errorMessage(error), "error");
    setBusy(actionButton, false);
  }
}

function bookingCard(booking) {
  const services = JSON.parse(booking.services_snapshot || "[]");
  const card = create("article", "owner-booking-card");
  const dateBlock = create("div", "owner-booking-card__date");
  dateBlock.append(
    create("span", "", new Intl.DateTimeFormat("he-IL", { weekday: "short" }).format(parseDate(booking.booking_date))),
    create("strong", "", String(parseDate(booking.booking_date).getDate())),
    create("small", "", new Intl.DateTimeFormat("he-IL", { month: "short" }).format(parseDate(booking.booking_date)))
  );
  const content = create("div", "owner-booking-card__content");
  const top = create("div", "owner-booking-card__top");
  const name = create("div");
  name.append(create("h3", "", booking.customer_name), create("a", "phone-link ltr", booking.customer_email));
  $("a", name).href = `mailto:${booking.customer_email}`;
  top.append(name, create("span", `status-badge status-badge--${booking.status}`, statusLabels[booking.status] || booking.status));
  const details = create("div", "owner-booking-card__details");
  details.append(
    detailItem("clock", `${booking.booking_time} · ${booking.duration_minutes} דק׳`),
    detailItem("briefcase", services.map((service) => service.name).join(", ")),
    detailItem("calendar-check", formatMoney(booking.price))
  );
  if (booking.notes) details.append(detailItem("message", booking.notes));
  const actions = create("div", "owner-booking-card__actions");
  if (booking.status === "pending") {
    const approve = button("אישור", () => updateBookingStatus(booking, "approved", approve), { className: "btn--primary btn--compact", icon: "check" });
    const reject = button("דחייה", () => updateBookingStatus(booking, "rejected", reject), { className: "btn--secondary btn--compact" });
    actions.append(approve, reject);
  }
  if (["pending", "approved"].includes(booking.status)) {
    const cancel = button("ביטול", () => updateBookingStatus(booking, "cancelled", cancel), { className: "btn--ghost btn--compact" });
    actions.append(cancel);
  }
  if (booking.status === "approved") {
    const arrival = button("בקשת אישור הגעה", async () => {
      try {
        await api(`/api/owner/bookings/${booking.id}/request-arrival`, { method: "POST" });
        toast("בקשת ההגעה נשלחה ללקוחה.");
        renderBookings();
      } catch (error) { toast(errorMessage(error), "error"); }
    }, { className: "btn--secondary btn--compact", icon: "send" });
    const noShow = button("לא הגיעה", async () => {
      if (!(await confirmAction("לסמן שהלקוחה לא הגיעה? הפעולה תעדכן גם את מונה אי־ההגעה שלה.", "סימון אי־הגעה", "סימון"))) return;
      try {
        await api(`/api/owner/bookings/${booking.id}/no-show`, { method: "POST" });
        toast("אי־ההגעה נרשמה.");
        renderBookings();
      } catch (error) { toast(errorMessage(error), "error"); }
    }, { className: "btn--ghost btn--compact" });
    actions.append(arrival, noShow);
  }
  content.append(top, details);
  if (actions.childElementCount) content.append(actions);
  card.append(dateBlock, content);
  return card;
}

function detailItem(iconName, text) {
  const item = create("span", "detail-item");
  item.append(icon(iconName), create("span", "", text));
  return item;
}

async function renderBookings() {
  const today = localIso();
  const data = await api(`/api/owner/bookings?date_from=${today}&date_to=2099-12-31`);
  const pending = data.bookings.filter((booking) => booking.status === "pending").length;
  const todayCount = data.bookings.filter((booking) => booking.booking_date === today).length;
  const approved = data.bookings.filter((booking) => booking.status === "approved").length;
  $("#stats").replaceChildren(
    statCard("ממתינים לאישור", pending, "clock", "warning"),
    statCard("תורים היום", todayCount, "calendar-days", "accent"),
    statCard("תורים מאושרים", approved, "calendar-check", "success")
  );
  const view = $("#view");
  view.replaceChildren();
  const section = create("section", "admin-section");
  section.append(sectionHeader("התורים הקרובים", data.bookings.length ? `${data.bookings.length} תורים מוצגים` : "אין תורים להצגה"));
  const list = create("div", "owner-booking-list");
  if (!data.bookings.length) list.append(emptyState("היומן פנוי", "תורים חדשים שיוזמנו יופיעו כאן."));
  else data.bookings.forEach((booking) => list.append(bookingCard(booking)));
  section.append(list);
  view.append(section);
}

function servicePayload(form) {
  return {
    name: value(form, "name").trim(),
    category: value(form, "category").trim() || null,
    price: Number(value(form, "price")),
    duration_minutes: Number(value(form, "duration_minutes")),
    display_order: Number(value(form, "display_order")),
    is_active: checked(form, "is_active"),
  };
}

function serviceForm(service = {}) {
  const form = create("form", "record-card service-form");
  const fields = create("div", "form-grid form-grid--services");
  fields.append(
    field("name", "שם השירות", service.name, { required: true, placeholder: "לדוגמה: טיפול פנים" }),
    field("category", "קטגוריה", service.category, { placeholder: "לא חובה" }),
    field("price", "מחיר", service.price ?? 0, { type: "number", min: 0, dir: "ltr" }),
    field("duration_minutes", "משך בדקות", service.duration_minutes ?? 60, { type: "number", min: 5, step: 5, dir: "ltr" }),
    field("display_order", "סדר הצגה", service.display_order ?? 0, { type: "number", min: 0, dir: "ltr" })
  );
  const footer = create("div", "record-card__footer");
  footer.append(switchField("is_active", "שירות פעיל", service.is_active ?? 1, "מוצג בעמוד קביעת התור"));
  const actions = create("div", "record-card__actions");
  const save = button(service.id ? "שמירה" : "הוספת שירות", async () => {
    if (!form.reportValidity()) return;
    setBusy(save, true);
    try {
      await api(service.id ? `/api/owner/services/${service.id}` : "/api/owner/services", {
        method: service.id ? "PUT" : "POST",
        body: JSON.stringify(servicePayload(form)),
      });
      toast(service.id ? "השירות נשמר." : "השירות נוסף.");
      renderServices();
    } catch (error) { toast(errorMessage(error), "error"); setBusy(save, false); }
  }, { className: "btn--primary btn--compact", icon: service.id ? "save" : "plus" });
  actions.append(save);
  if (service.id) {
    const remove = button("מחיקה", async () => {
      if (!(await confirmAction("למחוק את השירות? תורים קיימים ימשיכו להציג את פרטי השירות שנשמרו בהם.", "מחיקת שירות", "מחיקה"))) return;
      await api(`/api/owner/services/${service.id}`, { method: "DELETE" });
      toast("השירות נמחק.");
      renderServices();
    }, { className: "btn--ghost btn--compact", icon: "trash" });
    actions.append(remove);
  }
  footer.append(actions);
  form.append(fields, footer);
  form.addEventListener("submit", (event) => event.preventDefault());
  return form;
}

async function renderServices() {
  const data = await api("/api/owner/services");
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const add = create("details", "create-panel");
  const summary = create("summary");
  summary.append(icon("plus"), create("span", "", "שירות חדש"));
  add.append(summary, serviceForm());
  const section = create("section", "admin-section");
  section.append(sectionHeader("השירותים שלך", `${data.services.length} שירותים במערכת`, add));
  const list = create("div", "record-list");
  data.services.forEach((service) => list.append(serviceForm(service)));
  if (!data.services.length) list.append(emptyState("עדיין אין שירותים", "הוסיפי שירות ראשון כדי שלקוחות יוכלו לקבוע תור."));
  section.append(list);
  view.append(section);
}

function toggleHoursRow(row) {
  const closed = checked(row, "is_closed") === 1;
  $$('[name="open_time"], [name="close_time"]', row).forEach((input) => { input.disabled = closed; });
  row.classList.toggle("is-closed", closed);
}

async function renderHours() {
  const data = await api("/api/owner/hours");
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const section = create("section", "admin-section");
  section.append(sectionHeader("מערכת שעות שבועית", "סימון יום כסגור מונע קביעת תורים באותו יום."));
  const rows = create("div", "hours-list");
  data.hours.forEach((hours) => {
    const row = create("article", "hours-row");
    row.dataset.day = hours.day_of_week;
    const day = create("div", "hours-row__day");
    day.append(create("strong", "", dayNames[hours.day_of_week]), create("span", "", hours.is_closed ? "סגור" : "פתוח"));
    const controls = create("div", "hours-row__controls");
    controls.append(
      field("open_time", "פתיחה", hours.open_time, { type: "time", dir: "ltr" }),
      field("close_time", "סגירה", hours.close_time, { type: "time", dir: "ltr" }),
      field("slot_interval_minutes", "מרווח", hours.slot_interval_minutes, { type: "number", min: 5, step: 5, dir: "ltr" })
    );
    const closed = switchField("is_closed", "יום סגור", hours.is_closed);
    $("input", closed).addEventListener("change", () => toggleHoursRow(row));
    row.append(day, controls, closed);
    toggleHoursRow(row);
    rows.append(row);
  });
  const save = button("שמירת שעות", async () => {
    setBusy(save, true);
    try {
      const payload = $$(".hours-row", rows).map((row) => ({
        day_of_week: Number(row.dataset.day),
        is_closed: checked(row, "is_closed"),
        open_time: value(row, "open_time") || null,
        close_time: value(row, "close_time") || null,
        slot_interval_minutes: Number(value(row, "slot_interval_minutes")),
      }));
      await api("/api/owner/hours", { method: "PUT", body: JSON.stringify(payload) });
      toast("שעות הפעילות נשמרו.");
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { setBusy(save, false); }
  }, { className: "btn--primary", icon: "save" });
  section.append(rows, createActionBar(save));
  view.append(section);
}

function createActionBar(...items) {
  const bar = create("div", "form-action-bar");
  bar.append(...items);
  return bar;
}

function overrideForm() {
  const form = create("form", "create-form");
  const grid = create("div", "form-grid");
  grid.append(
    field("override_date", "תאריך", "", { type: "date", required: true, dir: "ltr" }),
    field("open_time", "שעת פתיחה", "09:00", { type: "time", dir: "ltr" }),
    field("close_time", "שעת סגירה", "18:00", { type: "time", dir: "ltr" }),
    field("slot_interval_minutes", "מרווח בדקות", "15", { type: "number", min: 5, step: 5, dir: "ltr" }),
    field("internal_note", "הערה פנימית", "", { placeholder: "לא מוצג ללקוחות" })
  );
  const closed = switchField("is_closed", "העסק סגור", false, "יום חופש מלא");
  const save = button("הוספה", async () => {
    if (!form.reportValidity()) return;
    setBusy(save, true);
    try {
      await api("/api/owner/overrides", {
        method: "POST",
        body: JSON.stringify({
          override_date: value(form, "override_date"),
          is_closed: checked(form, "is_closed"),
          open_time: value(form, "open_time") || null,
          close_time: value(form, "close_time") || null,
          slot_interval_minutes: Number(value(form, "slot_interval_minutes")) || null,
          internal_note: value(form, "internal_note") || null,
        }),
      });
      toast("היום המיוחד נוסף.");
      renderOverrides();
    } catch (error) { toast(errorMessage(error), "error"); setBusy(save, false); }
  }, { className: "btn--primary", icon: "plus" });
  form.append(grid, closed, save);
  form.addEventListener("submit", (event) => event.preventDefault());
  return form;
}

async function renderOverrides() {
  const data = await api("/api/owner/overrides");
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const createPanel = create("details", "create-panel");
  const summary = create("summary");
  summary.append(icon("plus"), create("span", "", "הוספת יום מיוחד"));
  createPanel.append(summary, overrideForm());
  const section = create("section", "admin-section");
  section.append(sectionHeader("לוח ימים מיוחדים", "יום מיוחד מחליף את שעות הפעילות הקבועות באותו תאריך.", createPanel));
  const list = create("div", "simple-list");
  data.overrides.forEach((item) => {
    const row = create("article", "simple-row");
    const mark = create("div", "simple-row__icon");
    mark.append(icon(item.is_closed ? "calendar-off" : "clock"));
    const copy = create("div", "simple-row__copy");
    copy.append(
      create("strong", "", formatDate(item.override_date, true)),
      create("span", "", item.is_closed ? "סגור כל היום" : `${item.open_time}–${item.close_time}`)
    );
    if (item.internal_note) copy.append(create("small", "", item.internal_note));
    const remove = button("מחיקה", async () => {
      if (!(await confirmAction("למחוק את היום המיוחד? המערכת תחזור לשעות הפעילות הקבועות.", "מחיקת יום מיוחד", "מחיקה"))) return;
      await api(`/api/owner/overrides/${item.override_date}`, { method: "DELETE" });
      toast("היום המיוחד נמחק.");
      renderOverrides();
    }, { className: "btn--ghost btn--compact", icon: "trash" });
    row.append(mark, copy, remove);
    list.append(row);
  });
  if (!data.overrides.length) list.append(emptyState("אין ימים מיוחדים", "הוסיפי יום רק כאשר שעות הפעילות שונות מהרגיל."));
  section.append(list);
  view.append(section);
}

function blockForm() {
  const form = create("form", "create-form");
  const grid = create("div", "form-grid");
  grid.append(
    field("blocked_date", "תאריך", "", { type: "date", required: true, dir: "ltr" }),
    field("blocked_time", "שעת התחלה", "", { type: "time", required: true, dir: "ltr" }),
    field("duration_minutes", "משך בדקות", "60", { type: "number", min: 5, step: 5, dir: "ltr" }),
    field("internal_note", "הערה פנימית", "", { placeholder: "לדוגמה: פגישה אישית" })
  );
  const save = button("חסימת הזמן", async () => {
    if (!form.reportValidity()) return;
    setBusy(save, true);
    try {
      await api("/api/owner/blocks", {
        method: "POST",
        body: JSON.stringify({
          blocked_date: value(form, "blocked_date"),
          blocked_time: value(form, "blocked_time"),
          duration_minutes: Number(value(form, "duration_minutes")),
          internal_note: value(form, "internal_note") || null,
        }),
      });
      toast("הזמן נחסם ביומן.");
      renderBlocks();
    } catch (error) { toast(errorMessage(error), "error"); setBusy(save, false); }
  }, { className: "btn--primary", icon: "ban" });
  form.append(grid, save);
  form.addEventListener("submit", (event) => event.preventDefault());
  return form;
}

async function renderBlocks() {
  const data = await api("/api/owner/blocks");
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const createPanel = create("details", "create-panel");
  const summary = create("summary");
  summary.append(icon("plus"), create("span", "", "חסימת זמן"));
  createPanel.append(summary, blockForm());
  const section = create("section", "admin-section");
  section.append(sectionHeader("זמנים חסומים", "הזמנים האלה לא יוצגו ללקוחות כשעות פנויות.", createPanel));
  const list = create("div", "simple-list");
  data.blocks.forEach((item) => {
    const row = create("article", "simple-row");
    const mark = create("div", "simple-row__icon simple-row__icon--danger");
    mark.append(icon("ban"));
    const copy = create("div", "simple-row__copy");
    copy.append(
      create("strong", "", formatDate(item.blocked_date, true)),
      create("span", "", `${item.blocked_time} · ${item.duration_minutes} דקות`)
    );
    if (item.internal_note) copy.append(create("small", "", item.internal_note));
    const remove = button("מחיקה", async () => {
      if (!(await confirmAction("להסיר את החסימה? הזמן עשוי לחזור ולהופיע כפנוי.", "הסרת חסימה", "הסרה"))) return;
      await api(`/api/owner/blocks/${item.id}`, { method: "DELETE" });
      toast("החסימה הוסרה.");
      renderBlocks();
    }, { className: "btn--ghost btn--compact", icon: "trash" });
    row.append(mark, copy, remove);
    list.append(row);
  });
  if (!data.blocks.length) list.append(emptyState("אין זמנים חסומים", "אפשר לחסום זמן לפגישה, הפסקה או סידור אישי."));
  section.append(list);
  view.append(section);
}

function customerCard(customer) {
  const form = create("form", "customer-card");
  const avatar = create("div", "customer-card__avatar", (customer.name || "?").trim().slice(0, 1));
  const header = create("div", "customer-card__header");
  const info = create("div");
  info.append(create("h3", "", customer.name), create("a", "phone-link ltr", customer.email));
  $("a", info).href = `mailto:${customer.email}`;
  header.append(avatar, info);
  if (customer.no_show_count) header.append(create("span", "status-badge status-badge--warning", `${customer.no_show_count} אי־הגעה`));
  const note = field("internal_note", "הערה פנימית", customer.internal_note, { multiline: true, rows: 2, placeholder: "ההערה גלויה רק לך" });
  const footer = create("div", "record-card__footer");
  footer.append(switchField("is_blocked", "חסימת קביעת תורים", customer.is_blocked, "הלקוחה לא תוכל לקבוע תור אונליין"));
  const save = button("שמירה", async () => {
    setBusy(save, true);
    try {
      await api(`/api/owner/customers/${customer.id}`, {
        method: "PUT",
        body: JSON.stringify({ internal_note: value(form, "internal_note") || null, is_blocked: checked(form, "is_blocked") }),
      });
      toast("פרטי הלקוחה נשמרו.");
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { setBusy(save, false); }
  }, { className: "btn--primary btn--compact", icon: "save" });
  footer.append(save);
  form.append(header, note, footer);
  form.addEventListener("submit", (event) => event.preventDefault());
  return form;
}

async function renderCustomers() {
  const data = await api("/api/owner/customers");
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const section = create("section", "admin-section");
  section.append(sectionHeader("לקוחות", `${data.customers.length} לקוחות במערכת`));
  const list = create("div", "customer-grid");
  data.customers.forEach((customer) => list.append(customerCard(customer)));
  if (!data.customers.length) list.append(emptyState("עדיין אין לקוחות", "לקוחות יופיעו כאן לאחר קביעת התור הראשונה שלהן."));
  section.append(list);
  view.append(section);
}

function settingsPayload(form) {
  return {
    name: value(form, "name").trim(),
    description: value(form, "description") || null,
    address: value(form, "address") || null,
    phone: value(form, "phone") || null,
    social_url: value(form, "social_url") || null,
    waze_url: value(form, "waze_url") || null,
    cover_image: value(form, "cover_image") || null,
    profile_image: value(form, "profile_image") || null,
    preparation_message: value(form, "preparation_message") || null,
    min_lead_minutes: Number(value(form, "min_lead_minutes")),
    max_days_ahead: Number(value(form, "max_days_ahead")),
  };
}

async function renderSettings() {
  const [settings, google] = await Promise.all([
    api("/api/owner/settings"),
    api("/api/owner/google/status"),
  ]);
  $("#stats").replaceChildren();
  const view = $("#view");
  view.replaceChildren();
  const form = create("form", "settings-form");
  const businessSection = create("section", "settings-section");
  businessSection.append(sectionHeader("פרטי העסק", "המידע הראשי שמופיע בראש עמוד קביעת התור."));
  const businessFields = create("div", "form-grid");
  businessFields.append(
    field("name", "שם העסק", settings.name, { required: true }),
    field("phone", "טלפון ליצירת קשר", settings.phone, { type: "tel", dir: "ltr" }),
    field("address", "כתובת", settings.address),
    field("description", "תיאור קצר", settings.description, { multiline: true, rows: 3, className: "field--wide" })
  );
  businessSection.append(businessFields);
  const linksSection = create("section", "settings-section");
  linksSection.append(sectionHeader("קישורים ותמונות", "אפשר להשאיר שדה ריק כדי שלא יוצג בעמוד הלקוחות."));
  const linkFields = create("div", "form-grid");
  linkFields.append(
    field("social_url", "קישור לרשת חברתית", settings.social_url, { type: "url", dir: "ltr" }),
    field("waze_url", "קישור Waze", settings.waze_url, { type: "url", dir: "ltr" }),
    field("cover_image", "כתובת תמונת קאבר", settings.cover_image, { type: "url", dir: "ltr" }),
    field("profile_image", "כתובת תמונת פרופיל", settings.profile_image, { type: "url", dir: "ltr" })
  );
  linksSection.append(linkFields);
  const bookingSection = create("section", "settings-section");
  bookingSection.append(sectionHeader("כללי הזמנה", "הגדרות שמשפיעות על המועדים שהמערכת מציעה."));
  const bookingFields = create("div", "form-grid");
  bookingFields.append(
    field("min_lead_minutes", "זמן מינימלי מראש בדקות", settings.min_lead_minutes, { type: "number", min: 0, dir: "ltr" }),
    field("max_days_ahead", "כמה ימים קדימה", settings.max_days_ahead, { type: "number", min: 1, max: 365, dir: "ltr" }),
    field("preparation_message", "הודעה לפני התור", settings.preparation_message, { multiline: true, rows: 3, className: "field--wide", placeholder: "לדוגמה: מומלץ להגיע 5 דקות לפני הזמן" })
  );
  bookingSection.append(bookingFields);
  const integrationsSection = create("section", "settings-section");
  const googleAction = google.oauth_ready
    ? button(google.connected ? "חיבור מחדש" : "חיבור Google Calendar", () => { window.location.href = "/api/owner/google/connect"; }, { className: google.connected ? "btn--secondary btn--compact" : "btn--primary btn--compact", icon: "calendar-check" })
    : null;
  integrationsSection.append(sectionHeader("חיבורים", google.connected ? `Google Calendar מחובר ליומן ${google.calendar_id}.` : "חברי את Google Calendar כדי שתורים מאושרים ייכנסו ליומן אוטומטית.", googleAction));
  if (google.connected) {
    const disconnect = button("ניתוק", async () => {
      if (!(await confirmAction("לנתק את Google Calendar? תורים חדשים לא ייכנסו ליומן עד לחיבור מחדש.", "ניתוק Google Calendar", "ניתוק"))) return;
      await api("/api/owner/google/disconnect", { method: "POST" });
      toast("Google Calendar נותק.");
      renderSettings();
    }, { className: "btn--ghost btn--compact" });
    integrationsSection.append(createActionBar(disconnect));
  } else if (!google.oauth_ready) {
    integrationsSection.append(errorBanner("חסרים GOOGLE_CLIENT_ID או GOOGLE_CLIENT_SECRET בקובץ .env."));
  }
  const save = button("שמירת הגדרות", async () => {
    if (!form.reportValidity()) return;
    setBusy(save, true);
    try {
      await api("/api/owner/settings", { method: "PUT", body: JSON.stringify(settingsPayload(form)) });
      toast("הגדרות העסק נשמרו.");
    } catch (error) { toast(errorMessage(error), "error"); }
    finally { setBusy(save, false); }
  }, { className: "btn--primary", icon: "save" });
  form.append(businessSection, linksSection, bookingSection, integrationsSection, createActionBar(save));
  form.addEventListener("submit", (event) => event.preventDefault());
  view.append(form);
}

function confirmAction(message, title = "אישור פעולה", acceptLabel = "אישור") {
  const dialog = $("#confirmDialog");
  $("#confirmTitle").textContent = title;
  $("#confirmMessage").textContent = message;
  $("#confirmAccept").textContent = acceptLabel;
  dialog.showModal();
  return new Promise((resolve) => {
    $("#confirmAccept").onclick = () => { dialog.close(); resolve(true); };
    $("#confirmCancel").onclick = () => { dialog.close(); resolve(false); };
    dialog.oncancel = () => resolve(false);
  });
}

async function requestCode() {
  const buttonNode = $("#sendCode");
  const email = $("#email").value.trim();
  if (!email) {
    $("#loginStatus").textContent = "הזיני כתובת מייל כדי לקבל קוד.";
    $("#email").focus();
    return;
  }
  setBusy(buttonNode, true, "שולחת...");
  try {
    await api("/api/auth/request-code", { method: "POST", body: JSON.stringify({ email }) });
    $("#ownerCodeField").hidden = false;
    $("#loginStatus").textContent = "הקוד נשלח. הוא תקף למשך 5 דקות.";
    $("#code").focus();
  } catch (error) {
    $("#loginStatus").textContent = errorMessage(error);
  } finally {
    setBusy(buttonNode, false);
  }
}

async function verifyCode() {
  const buttonNode = $("#verify");
  setBusy(buttonNode, true, "בודקת...");
  try {
    const result = await api("/api/auth/verify", {
      method: "POST",
      body: JSON.stringify({ email: $("#email").value, code: $("#code").value }),
    });
    if (result.role !== "owner") throw new Error("כתובת המייל הזו אינה מורשית להיכנס לניהול.");
    showAdmin();
  } catch (error) {
    $("#loginStatus").textContent = errorMessage(error);
    $("#code").select();
  } finally {
    setBusy(buttonNode, false);
  }
}

$$('[data-tab]').forEach((item) => item.addEventListener("click", () => switchTab(item.dataset.tab)));
$("#sendCode").addEventListener("click", requestCode);
$("#verify").addEventListener("click", verifyCode);

api("/api/me")
  .then((me) => { if (me.role === "owner") showAdmin(); })
  .catch(() => {});
