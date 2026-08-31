/* 财宝资本官网 i18n: 中英文切换 (localStorage 持久化)
 *
 * 用法:
 *   1. 各页面 <head> 引入本脚本 (须在 DOMContentLoaded 之前)
 *   2. 通用文案: <span data-i18n="key">...原文...</span>
 *      placeholder:  <input data-i18n-ph="key">
 *      document.title: <title data-i18n-title="key">
 *   3. 页面特有文案: 在页面 DOMContentLoaded 前调用
 *      window.I18N.register({ zh: {...}, en: {...} });
 *   4. 动态文本 (JS 渲染后): 注册回调
 *      window.I18N.onApply = function (lang) { ...重新渲染... };
 *
 * 语言切换按钮自动注入到 .nav-inner 末尾, 点击切换 中 / EN。
 * 偏好保存于 localStorage["caibao_lang"], 同浏览器下次访问保持。
 */
(function () {
  "use strict";

  var LS_KEY = "caibao_lang";

  /* 通用词典 (导航 / 页脚 / 列表 / 文章公用文案) */
  var DICT = {
    zh: {
      "brand.name": "财宝资本",
      "brand.sub": "CaiBao Capital",
      "nav.culture": "公司文化",
      "nav.research": "行业研究",
      "nav.news": "新闻浏览",
      "footer.capital": "财宝资本",
      "footer.desc": "创造时间和知识的复利，一视同仁地为客户创造合理的、可持续的、超过社会平均的收益。",
      "footer.nav": "快捷导航",
      "footer.culture": "公司文化",
      "footer.research": "行业研究",
      "footer.news": "新闻浏览",
      "footer.contact": "联系我们",
      "footer.email": "邮箱：contact@caibaocapital.com",
      "footer.tel": "电话：+86 000 0000 0000",
      "footer.addr": "地址：中国 · 上海",
      "footer.rights": "© 2026 财宝资本 CaiBao Capital. 保留所有权利。",
      "footer.disclaimer": "本站内容仅供研究参考，不构成投资建议。",
      "list.loading": "加载中…",
      "list.empty": "暂无内容",
      "list.fail": "加载失败",
      "article.notfound": "文章不存在",
      "article.deleted": "文章不存在或已删除",
      "back.list": "← 返回列表"
    },
    en: {
      "brand.name": "财宝资本",
      "brand.sub": "CaiBao Capital",
      "nav.culture": "Culture",
      "nav.research": "Research",
      "nav.news": "News",
      "footer.capital": "CaiBao Capital",
      "footer.desc": "Create compounding of time and knowledge. Serve every client impartially, with reasonable, sustainable returns above the social average.",
      "footer.nav": "Quick Links",
      "footer.culture": "Culture",
      "footer.research": "Research",
      "footer.news": "News",
      "footer.contact": "Contact Us",
      "footer.email": "Email: contact@caibaocapital.com",
      "footer.tel": "Tel: +86 000 0000 0000",
      "footer.addr": "Address: Shanghai, China",
      "footer.rights": "© 2026 CaiBao Capital. All rights reserved.",
      "footer.disclaimer": "Content for research reference only, not investment advice.",
      "list.loading": "Loading…",
      "list.empty": "No content yet",
      "list.fail": "Failed to load",
      "article.notfound": "Article not found",
      "article.deleted": "Article not found or deleted",
      "back.list": "← Back to list"
    }
  };

  var pageDicts = [];
  var current = localStorage.getItem(LS_KEY) === "en" ? "en" : "zh";

  function t(key) {
    if (!key) return "";
    var i;
    for (i = pageDicts.length - 1; i >= 0; i--) {
      if (pageDicts[i][current] && pageDicts[i][current][key] != null) {
        return pageDicts[i][current][key];
      }
    }
    if (DICT[current] && DICT[current][key] != null) return DICT[current][key];
    if (DICT.zh[key] != null) return DICT.zh[key];
    return key;
  }

  function register(dict) { if (dict) pageDicts.push(dict); }

  function getLang() { return current; }

  function apply() {
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    document.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      document.title = t(el.getAttribute("data-i18n-title"));
    });
    if (window.I18N && typeof window.I18N.onApply === "function") {
      window.I18N.onApply(current);
    }
  }

  function updateSwitcher() {
    var sw = document.getElementById("lang-switch");
    if (!sw) return;
    var en = current === "en";
    sw.textContent = en ? "中" : "EN";
    sw.title = en ? "切换到中文 / Switch to Chinese" : "切换到英文 / Switch to English";
  }

  function setLang(lang) {
    current = lang === "en" ? "en" : "zh";
    localStorage.setItem(LS_KEY, current);
    document.documentElement.setAttribute("lang", current === "en" ? "en" : "zh-CN");
    apply();
    updateSwitcher();
  }

  /* 语言切换按钮: 注入 .nav-inner 末尾 (无则挂 body) */
  function injectSwitcher() {
    var sw = document.createElement("button");
    sw.type = "button";
    sw.id = "lang-switch";
    sw.className = "lang-switch";
    sw.setAttribute("aria-label", "Language / 语言");
    sw.addEventListener("click", function () {
      setLang(current === "en" ? "zh" : "en");
    });
    var host = document.querySelector(".nav-inner");
    if (host) host.appendChild(sw);
    else document.body.appendChild(sw);
    updateSwitcher();
  }

  function init() {
    document.documentElement.setAttribute("lang", current === "en" ? "en" : "zh-CN");
    injectSwitcher();
    apply();
    updateSwitcher();
  }

  window.I18N = {
    t: t,
    register: register,
    getLang: getLang,
    setLang: setLang,
    apply: apply,
    init: init,
    onApply: null
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();