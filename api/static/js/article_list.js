/* 行业研究 / 新闻浏览列表页: 服务端分页 + 标签筛选 + 加载状态
 *
 * 用法: initArticleList({ kind: "research", listId: "research-list",
 *                       tagsId: "research-tags", pagerId: "research-pager",
 *                       pageSize: 10 });
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 解析标签 JSON (兼容空值/脏数据)
  function tagsOf(it) {
    var arr = [];
    try { arr = JSON.parse(it.tags || "[]") || []; } catch (e) { arr = []; }
    return Array.isArray(arr) ? arr.map(function (x) { return String(x); }) : [];
  }

  function init(opts) {
    opts = opts || {};
    var kind = opts.kind === "news" ? "news" : "research";
    var pageSize = opts.pageSize || 10;
    var box = document.getElementById(opts.listId);
    var filterBox = document.getElementById(opts.tagsId);
    var pagerBox = document.getElementById(opts.pagerId);
    if (!box) return;

    var page = 1;
    var activeTag = "";
    var total = 0;
    var loading = false;

    function showState(text) {
      box.innerHTML = '<div class="loading-inline">' + esc(text) + "</div>";
      if (pagerBox) pagerBox.innerHTML = "";
    }

    function renderChips(tags) {
      if (!filterBox) return;
      if (!tags || !tags.length) { filterBox.innerHTML = ""; return; }
      var chips = [{ name: "", count: total }].concat(tags.map(function (t) {
        return { name: t.name, count: t.count };
      }));
      filterBox.innerHTML = chips.map(function (t) {
        var label = t.name || "全部";
        return '<button type="button" class="tag-chip' + (t.name === activeTag ? " active" : "") +
          '" data-tag="' + esc(t.name) + '">' + esc(label) +
          '<span class="cnt">' + t.count + "</span></button>";
      }).join("");
      Array.prototype.forEach.call(filterBox.querySelectorAll("[data-tag]"), function (btn) {
        btn.addEventListener("click", function () {
          var tag = btn.getAttribute("data-tag") || "";
          if (tag === activeTag) return;
          activeTag = tag;
          page = 1;
          load();
        });
      });
    }

    function renderList(items) {
      if (!items.length) {
        showState("暂无内容");
        return;
      }
      box.innerHTML = items.map(function (it) {
        var tagHtml = tagsOf(it).map(function (x) {
          return '<span class="news-tag">' + esc(x) + "</span>";
        }).join("");
        return (
          '<a class="article-item" href="/article/' + it.id + '">' +
          '  <div class="article-head">' +
          '    <span class="news-date">' + esc(it.date || "") + "</span>" +
          '    <span class="article-title">' + esc(it.title) + "</span>" +
          tagHtml +
          "  </div>" +
          "</a>"
        );
      }).join("");
    }

    function pageList(cur, totalPages) {
      var out = [];
      var start = Math.max(1, cur - 2);
      var end = Math.min(totalPages, cur + 2);
      if (start > 1) {
        out.push(1);
        if (start > 2) out.push("...");
      }
      for (var i = start; i <= end; i++) out.push(i);
      if (end < totalPages) {
        if (end < totalPages - 1) out.push("...");
        out.push(totalPages);
      }
      return out;
    }

    function renderPager() {
      if (!pagerBox) return;
      var totalPages = Math.max(1, Math.ceil(total / pageSize));
      if (totalPages <= 1) { pagerBox.innerHTML = ""; return; }
      var nums = pageList(page, totalPages).map(function (p) {
        if (p === "...") return '<span class="pager-gap">…</span>';
        return '<button type="button" class="page-btn' + (p === page ? " active" : "") +
          '" data-page="' + p + '">' + p + "</button>";
      }).join("");
      pagerBox.innerHTML =
        '<nav class="pager" aria-label="分页">' +
        '<button type="button" class="page-btn page-prev" data-page="' + (page - 1) + '"' +
        (page <= 1 ? " disabled" : "") + ">上一页</button>" +
        nums +
        '<button type="button" class="page-btn page-next" data-page="' + (page + 1) + '"' +
        (page >= totalPages ? " disabled" : "") + ">下一页</button>" +
        '<span class="pager-info">第 ' + page + " / " + totalPages + " 页 · 共 " + total + " 条</span>" +
        "</nav>";
      Array.prototype.forEach.call(pagerBox.querySelectorAll("[data-page]"), function (btn) {
        btn.addEventListener("click", function () {
          var p = parseInt(btn.getAttribute("data-page"), 10);
          if (!p || p === page || p < 1 || p > totalPages) return;
          page = p;
          load();
          var main = document.getElementById(opts.tagsId);
          if (main && main.scrollIntoView) main.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      });
    }

    function load() {
      if (loading) return;
      loading = true;
      showState("加载中…");
      var url = "/api/site/articles?kind=" + encodeURIComponent(kind) +
        "&page=" + page + "&page_size=" + pageSize +
        (activeTag ? "&tag=" + encodeURIComponent(activeTag) : "");
      fetch(url)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("status " + r.status)); })
        .then(function (d) {
          total = d.total || 0;
          renderChips(d.tags || []);
          renderList(d.items || []);
          renderPager();
        })
        .catch(function () { showState("加载失败"); })
        .finally(function () { loading = false; });
    }

    load();
  }

  window.initArticleList = init;
})();
