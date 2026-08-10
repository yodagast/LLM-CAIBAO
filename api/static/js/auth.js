/* 用户认证 (登录/注册/注销) + 顶部用户状态。
   index.html 与 stock_detail.html 共用:
     - 页面需包含 <div id="auth-zone"> (用户状态按钮区) 与登录弹窗元素 (auth-modal 等)
     - 调用 CaiBaoAuth.init() 初始化; CaiBaoAuth.requireLogin() 未登录时弹出登录框
*/
(function () {
  "use strict";

  var user = null;      // {id, username} | null
  var mode = "login";   // login | register
  var changeCb = null;

  function $(s) { return document.querySelector(s); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 普通页面 (首页/详情页) 顶栏用户区: 未登录=登录, 已登录=用户名(进个人页)+退出
  function renderZone() {
    var zone = $("#auth-zone");
    if (!zone) return;
    if (user) {
      zone.innerHTML =
        '<a class="auth-user" href="/static/user.html" title="个人中心">👤 ' + esc(user.username) + "</a>" +
        '<button type="button" class="auth-link ghost" data-aa="logout">退出</button>';
    } else {
      zone.innerHTML =
        '<button type="button" class="auth-link" data-aa="login">登录</button>';
    }
    zone.querySelectorAll("[data-aa]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        onAction(btn.getAttribute("data-aa"));
      });
    });
  }

  function onAction(a) {
    if (a === "login") openModal("login");
    else if (a === "register") openModal("register");
    else if (a === "logout") doLogout();
  }

  function openModal(m) {
    mode = m || "login";
    var modal = $("#auth-modal");
    if (!modal) return;
    modal.classList.remove("hidden");
    updateModalUI();
    var u = $("#auth-username");
    if (u) setTimeout(function () { u.focus(); }, 30);
  }
  function closeModal() {
    var modal = $("#auth-modal");
    if (modal) modal.classList.add("hidden");
  }
  function updateModalUI() {
    var isReg = mode === "register";
    var title = $("#auth-title");
    if (title) title.textContent = isReg ? "注册新账号" : "登录";
    var cw = $("#auth-confirm-wrap");
    if (cw) cw.classList.toggle("hidden", !isReg);
    var sub = $("#auth-submit");
    if (sub) sub.textContent = isReg ? "注册" : "登录";
    var tg = $("#auth-toggle");
    if (tg) tg.textContent = isReg ? "已有账号? 去登录" : "没有账号? 去注册";
    var err = $("#auth-error");
    if (err) err.classList.add("hidden");
  }
  function toggleMode() {
    mode = mode === "login" ? "register" : "login";
    updateModalUI();
  }

  function fail(msg) {
    var err = $("#auth-error");
    if (err) { err.textContent = msg; err.classList.remove("hidden"); }
  }

  async function submit() {
    var u = $("#auth-username"), p = $("#auth-password"), cf = $("#auth-confirm");
    var username = (u && u.value || "").trim();
    var password = p ? p.value : "";
    if (!username) return fail("请输入用户名");
    if (password.length < 6) return fail("密码至少 6 位");
    if (mode === "register" && password !== (cf ? cf.value : "")) {
      return fail("两次输入的密码不一致");
    }
    var btn = $("#auth-submit");
    if (btn) btn.disabled = true;
    try {
      var res = await fetch(mode === "register" ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username, password: password }),
      });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail || "操作失败");
      user = data.user;
      if (u) u.value = "";
      if (p) p.value = "";
      if (cf) cf.value = "";
      closeModal();
      renderZone();
      if (changeCb) changeCb(user);
    } catch (e) {
      fail(e.message || "请求失败");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function doLogout() {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) { /* 忽略 */ }
    user = null;
    renderZone();
    if (changeCb) changeCb(null);
  }

  async function me() {
    try {
      var r = await fetch("/api/auth/me");
      var d = await r.json();
      user = d && d.user ? d.user : null;
    } catch (e) { user = null; }
    return user;
  }

  function isLoggedIn() { return !!user; }
  function requireLogin() {
    if (user) return true;
    openModal("login");
    return false;
  }
  function getUser() { return user; }
  function onAuthChange(cb) { changeCb = cb; }

  async function init() {
    await me();
    renderZone();
    var sub = $("#auth-submit");
    if (sub) sub.addEventListener("click", submit);
    var tg = $("#auth-toggle");
    if (tg) tg.addEventListener("click", toggleMode);
    var cancel = $("#auth-cancel");
    if (cancel) cancel.addEventListener("click", closeModal);
    var mask = $("#auth-modal");
    if (mask) mask.addEventListener("click", function (e) { if (e.target === mask) closeModal(); });
    ["auth-username", "auth-password", "auth-confirm"].forEach(function (id) {
      var el = $("#" + id);
      if (el) el.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    });
  }

  // ---------- 用户个人中心页 (user.html) ----------
  async function initUserPage() {
    await me();
    if (!user) { location.href = "/"; return; }
    renderUserPage();
  }
  function renderUserPage() {
    var el = $("#user-content");
    if (!el) return;
    el.innerHTML =
      '<div class="user-avatar">👤</div>' +
      "<h2>" + esc(user.username) + "</h2>" +
      '<p class="user-meta">注册时间: ' + esc(user.created_at || "—") + "</p>" +
      '<p class="user-meta">绑定自选股将随账号删除</p>' +
      '<div class="user-actions">' +
      '  <a class="btn-ghost" href="/">返回首页</a>' +
      '  <button type="button" class="btn-danger" id="btn-delete-account">注销账号</button>' +
      "</div>";
    var del = $("#btn-delete-account");
    if (del) del.addEventListener("click", deleteAccount);
  }
  async function deleteAccount() {
    if (!window.confirm("确定要注销账号吗？该操作将永久删除账号及其全部自选股数据，不可恢复！")) return;
    var btn = $("#btn-delete-account");
    if (btn) btn.disabled = true;
    try {
      var res = await fetch("/api/auth/delete_account", { method: "POST" });
      if (!res.ok) {
        var d = await res.json().catch(function () { return {}; });
        throw new Error(d.detail || "注销失败");
      }
      user = null;
      location.href = "/";
    } catch (e) {
      window.alert(e.message || "注销失败");
      if (btn) btn.disabled = false;
    }
  }

  window.CaiBaoAuth = {
    init: init, me: me, render: renderZone,
    openModal: openModal, closeModal: closeModal,
    isLoggedIn: isLoggedIn, requireLogin: requireLogin,
    getUser: getUser, onAuthChange: onAuthChange, doLogout: doLogout,
    initUserPage: initUserPage, deleteAccount: deleteAccount,
  };
})();
