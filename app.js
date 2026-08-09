/* 知乎搜索索引前端逻辑
 * 使用 MiniSearch 对 data/index.json 中的元数据做模糊搜索。
 * 仅展示元数据（标题/摘要/作者/链接），不展示全文内容。
 */

(function () {
  "use strict";

  var INDEX_URL = "data/index.json";
  var INDEX_GZ_URL = "data/index.json.gz";
  var input = document.getElementById("search-input");
  var resultList = document.getElementById("results");
  var countEl = document.getElementById("result-count");
  var updatedEl = document.getElementById("updated-at");

  var allItems = [];
  var mini = null;

  // 优先加载 gzip 压缩版索引并在线解压，可容纳更多内容；
  // 若浏览器不支持或请求失败，回退到未压缩的 index.json。
  // 超大规模时 index.json 为清单（shards 列表），逐片加载后合并。
  function fetchJson(url) {
    return fetch(url).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var ct = (resp.headers.get("content-type") || "").toLowerCase();
      // 若服务器已按 gzip 解压或返回纯 JSON，直接解析
      if (ct.indexOf("gzip") < 0 && ct.indexOf("json") >= 0) {
        return resp.json();
      }
      return resp.arrayBuffer().then(function (buf) {
        if (typeof DecompressionStream === "undefined") {
          throw new Error("浏览器不支持 gzip 解压");
        }
        var ds = new DecompressionStream("gzip");
        return new Response(new Blob([buf]).stream().pipeThrough(ds)).json();
      });
    });
  }

  function fetchShard(url) {
    // .gz 结尾的分片走 gzip 在线解压，普通 JSON 直接解析
    if (/\.gz$/i.test(url)) return fetchJson(url);
    return fetch(url).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    });
  }

  function loadIndex() {
    function parseJson(data) {
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
    }

    // 先读清单：分片模式（超大规模）或全量模式都从这里开始
    fetch("data/index.json")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (manifest) {
        if (manifest.items) { // 兼容旧版全量索引
          parseJson(manifest);
          return;
        }
        var shards = manifest.shards || [];
        if (shards.length === 0) throw new Error("index.json 缺少 items/shards");
        return Promise.all(shards.map(fetchShard)).then(function (parts) {
          var merged = {
            generated_at: manifest.generated_at,
            count: manifest.count,
            items: [],
          };
          parts.forEach(function (part) {
            merged.items = merged.items.concat(part);
          });
          parseJson(merged);
        });
      })
      // 旧部署兜底：清单读取失败时回退到全量 gzip / 未压缩 JSON
      .catch(function () {
        return fetchJson(INDEX_GZ_URL).then(parseJson);
      })
      .catch(function () {
        return fetchJson(INDEX_URL).then(parseJson);
      })
      .catch(function (err) {
        countEl.textContent = "索引加载失败：" + err.message;
        updatedEl.textContent = "";
      });
  }

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
    loadIndex();
  }

  input.addEventListener("input", function () {
    search(input.value.trim());
  });

  init();
})();
