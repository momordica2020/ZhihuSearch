/* 知乎搜索索引前端逻辑
 * 使用 MiniSearch 对 data/index.json 中的元数据做模糊搜索。
 * 仅展示元数据（标题/摘要/作者/链接），不展示全文内容。
 */

(function () {
  "use strict";

  var INDEX_URL = "data/index.json";
  var input = document.getElementById("search-input");
  var resultList = document.getElementById("results");
  var countEl = document.getElementById("result-count");
  var updatedEl = document.getElementById("updated-at");

  var allItems = [];
  var mini = null;

  // Mark 高亮：把命中词包上 <mark>
  function highlight(text, terms) {
    if (!terms || terms.length === 0) return text;
    var escaped = text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var pattern = terms
      .map(function (t) {
        return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      })
      .filter(Boolean)
      .join("|");
    if (!pattern) return text;
    var re = new RegExp("(" + pattern + ")", "gi");
    return escaped.replace(re, "<mark>$1</mark>");
  }

  function formatTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    return d.toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  }

  function render(items, terms) {
    resultList.innerHTML = "";
    if (!items || items.length === 0) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = allItems.length === 0
        ? "索引为空，请先运行爬虫生成 data/index.json"
        : "没有找到匹配的结果";
      resultList.appendChild(empty);
      return;
    }

    items.forEach(function (item) {
      var card = document.createElement("div");
      card.className = "result-card";

      var title = document.createElement("h2");
      title.className = "result-title";
      var link = document.createElement("a");
      link.href = item.url || "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.innerHTML = highlight(item.title || "无标题", terms);
      title.appendChild(link);

      var excerpt = document.createElement("p");
      excerpt.className = "result-excerpt";
      excerpt.innerHTML = highlight(item.excerpt || "", terms);

      var meta = document.createElement("div");
      meta.className = "result-meta";

      var badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.kind || "内容";

      var info = document.createElement("span");
      var parts = [];
      if (item.author) parts.push(item.author);
      if (item.votes) parts.push("赞 " + item.votes);
      if (item.comments) parts.push("评 " + item.comments);
      var t = formatTime(item.time);
      if (t) parts.push(t);
      info.textContent = parts.join(" · ");

      meta.appendChild(badge);
      meta.appendChild(info);

      card.appendChild(title);
      card.appendChild(excerpt);
      card.appendChild(meta);
      resultList.appendChild(card);
    });
  }

  function search(query) {
    if (!mini || !query) {
      render([], []);
      countEl.textContent = allItems.length
        ? "共收录 " + allItems.length + " 条"
        : "请稍候，正在加载索引…";
      return;
    }
    var results = mini.search(query, {
      fuzzy: 0.2,
      prefix: true,
      boost: { title: 3, excerpt: 1, author: 2 },
    });
    // 使用命中结果自带的关键词，用于高亮
    var terms = [];
    results.forEach(function (r) {
      (r.terms || []).forEach(function (t) {
        if (terms.indexOf(t) < 0) terms.push(t);
      });
    });
    render(results, terms);
    countEl.textContent = "找到 " + results.length + " 条结果";
  }

  function init() {
    fetch(INDEX_URL)
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        allItems = data.items || [];
        mini = new MiniSearch({
          idField: "url",
          fields: ["title", "excerpt", "author"],
          storeFields: ["kind", "title", "excerpt", "author", "url", "votes", "comments", "time"],
        });
        mini.addAll(allItems);
        if (data.generated_at) {
          updatedEl.textContent = "更新于 " + data.generated_at;
        }
        countEl.textContent = "共收录 " + allItems.length + " 条";
        if (input.value) search(input.value);
      })
      .catch(function (err) {
        countEl.textContent = "索引加载失败：" + err.message;
        updatedEl.textContent = "";
      });
  }

  input.addEventListener("input", function () {
    search(input.value.trim());
  });

  init();
})();
