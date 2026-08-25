/* 用户认证 (登录/注册/注销) + 顶部用户状态。
   index.html / stock_detail.html / login.html / user.html 共用:
     - 普通页 (首页/详情页) 需 <div id="auth-zone">; 未登录的普通页应跳转到登录页
     - login.html 为独立登录/注册页 (无 Tab 标签), 登录成功跳回 next
     - CaiBaoAuth.init() 初始化; requireLogin() 未登录跳转登录页
*/
(function () {
  "use strict";

  var user = null;      // {id, username} | null
  var mode = "login";   // login | register
  var changeCb = null;
  var loginPage = false;    // 是否在独立登录页
  var loginNext = "/admin"; // 登录成功后的跳转地址 (后台)

  function $(s) { return document.querySelector(s); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 普通页面 (首页/详情页) 顶栏用户区 (参考主流产品账号体系):
  //   未登录 = [登录](描边次按钮) + [注册](实心主按钮)
  //   已登录 = 圆形首字符头像 + 昵称 → 点击弹下拉菜单 (个人中心 / 退出登录)
  //   注销账号仅在个人中心页 (user.html) 提供 (防误触)
  function loginUrl(reg) {
    var q = (reg ? "?mode=register" : "");
    if (location.pathname !== "/") {
      q += (q ? "&" : "?") + "next=" + encodeURIComponent(location.pathname + location.search);
    }
    return "/static/login.html" + q;
  }
  function renderZone() {
    var zone = $("#auth-zone");
    if (!zone) return;
    if (user) {
      var initial = (user.username || "?").charAt(0).toUpperCase();
      zone.innerHTML =
        '<div class="auth-user" id="auth-user" role="button" tabindex="0" title="账号菜单">' +
        '  <span class="auth-avatar">' + esc(initial) + "</span>" +
        '  <span class="auth-name">' + esc(user.username) + "</span>" +
        '  <span class="auth-caret">▾</span>' +
        "</div>" +
        '<div class="auth-menu hidden" id="auth-menu">' +
        '  <div class="auth-menu-header">' + esc(user.username) + "</div>" +
        '  <a class="auth-menu-item" href="/static/user.html">个人中心</a>' +
        '  <button type="button" class="auth-menu-item danger" data-aa="logout">退出登录</button>' +
        "</div>";
      var trigger = $("#auth-user"), menu = $("#auth-menu");
      if (trigger && menu) {
        trigger.addEventListener("click", function (e) {
          e.stopPropagation();
          menu.classList.toggle("hidden");
        });
        trigger.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); menu.classList.toggle("hidden"); }
        });
      }
    } else {
      zone.innerHTML =
        '<button type="button" class="auth-link ghost" data-aa="login">登录</button>' +
        '<button type="button" class="auth-link" data-aa="register">注册</button>';
    }
    zone.querySelectorAll("[data-aa]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        onAction(btn.getAttribute("data-aa"));
      });
    });
  }

  function onAction(a) {
    if (a === "login") {
      location.href = loginUrl(false);
    } else if (a === "register") {
      location.href = loginUrl(true);
    } else if (a === "logout") {
      doLogout();
    }
  }

  function updateModalUI() {
    var isReg = mode === "register";
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
      if (loginPage) {
        location.href = loginNext || "/admin";
        return;
      }
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
    location.href = "/static/login.html?next=" +
      encodeURIComponent(location.pathname + location.search);
    return false;
  }
  function getUser() { return user; }
  function onAuthChange(cb) { changeCb = cb; }

  // ---------- 独立登录/注册页 (login.html) ----------
  async function initLoginPage() {
    loginPage = true;
    var q = new URLSearchParams(location.search);
    loginNext = q.get("next") || "/admin";
    if (q.get("mode") === "register") mode = "register";  // 右上角「注册」直达注册态
    await me();
    if (user) { location.href = loginNext; return; }
    updateModalUI();
    var sub = $("#auth-submit");
    if (sub) sub.addEventListener("click", submit);
    var tg = $("#auth-toggle");
    if (tg) tg.addEventListener("click", toggleMode);
    ["auth-username", "auth-password", "auth-confirm"].forEach(function (id) {
      var el = $("#" + id);
      if (el) el.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    });
  }

  async function init() {
    await me();
    renderZone();
    // 点击账号区外部 / 按 Esc 关闭下拉菜单 (只绑定一次)
    document.addEventListener("click", function (e) {
      var menu = $("#auth-menu");
      if (!menu || menu.classList.contains("hidden")) return;
      if (!(e.target.closest && e.target.closest("#auth-zone"))) menu.classList.add("hidden");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        var menu = $("#auth-menu");
        if (menu) menu.classList.add("hidden");
      }
    });
    var sub = $("#auth-submit");
    if (sub) sub.addEventListener("click", submit);
    var tg = $("#auth-toggle");
    if (tg) tg.addEventListener("click", toggleMode);
    ["auth-username", "auth-password", "auth-confirm"].forEach(function (id) {
      var el = $("#" + id);
      if (el) el.addEventListener("keydown", function (e) { if (e.key === "Enter") submit(); });
    });
  }

  // ---------- 用户个人中心页 (user.html) ----------
  async function initUserPage() {
    await me();
    if (!user) { location.href = "/admin"; return; }
    renderUserPage();
  }
  function renderUserPage() {
    var el = $("#user-content");
    if (!el) return;
    el.innerHTML =
      '<div class="user-avatar">' + esc((user.username || "?").charAt(0).toUpperCase()) + "</div>" +
      "<h2>" + esc(user.username) + "</h2>" +
      '<p class="user-meta">注册时间: ' + esc(user.created_at || "—") + "</p>" +
      '<p class="user-meta">绑定自选股将随账号删除</p>' +
      '<div class="user-actions">' +
      '  <a class="btn-ghost" href="/admin">返回后台</a>' +
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
      location.href = "/admin";
    } catch (e) {
      window.alert(e.message || "注销失败");
      if (btn) btn.disabled = false;
    }
  }

  window.CaiBaoAuth = {
    init: init, me: me, render: renderZone,
    isLoggedIn: isLoggedIn, requireLogin: requireLogin,
    getUser: getUser, onAuthChange: onAuthChange, doLogout: doLogout,
    initUserPage: initUserPage, deleteAccount: deleteAccount,
    initLoginPage: initLoginPage,
  };
})();
