const data = JSON.parse(document.getElementById("payload").textContent);
const app = document.getElementById("app");
const TYPE_LABEL = { word: "单词", phrase: "短语" };
const STATUS_LABEL = { candidate: "待决定", learning: "在背", known: "已认识", ignored: "忽略" };

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (value === false || value == null) return;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : value);
  });
  (children || []).filter(Boolean).forEach((child) => node.append(child));
  return node;
}

function formatTime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const clock = `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return hours ? `${hours}:${clock}` : clock;
}

function hintFor(item) {
  const surface = (item.surface || "").trim();
  if (!surface || surface.toLowerCase() === String(item.key).toLowerCase()) return "";
  return `片中：${surface}`;
}

function renderIndex() {
  const stats = data.stats || {};
  const videoFilter = el("select", { "aria-label": "按视频筛选" }, [
    el("option", { value: "", text: "全部视频" }),
    ...(data.videos || []).map((video) =>
      el("option", {
        value: video.slug,
        text: `${video.title} · 在背 ${video.learning} · 待决定 ${video.candidates}`,
      })
    ),
  ]);
  const showCandidates = el("input", { type: "checkbox", id: "show-candidates" });
  const list = el("div", { class: "row-list" });
  const countLine = el("p", { class: "sub" });

  function passes(item) {
    if (item.status === "candidate" && !showCandidates.checked) return false;
    if (item.status !== "learning" && item.status !== "candidate") return false;
    if (videoFilter.value && !(item.videos || []).some((video) => video.slug === videoFilter.value)) return false;
    return true;
  }

  function renderList() {
    const rows = (data.items || []).filter(passes);
    countLine.textContent = `当前 ${rows.length} 条`;
    list.replaceChildren();
    if (!rows.length) {
      list.append(
        el("p", {
          class: "empty",
          text: showCandidates.checked
            ? "没有待决定或在背的条目。"
            : "还没有在背的条目。打开某支视频的单词卡，点「加入背诵」，导出标记后再让我导入。",
        })
      );
      return;
    }
    rows.forEach((item) => {
      const first = (item.videos || [])[0];
      const links = (item.videos || []).map((video) =>
        el("a", {
          href: `${video.slug}.html#${encodeURIComponent(item.key)}`,
          text: video.title,
        })
      );
      const linkRow = el("div", { class: "meta" }, links.length ? links : [document.createTextNode("还没有挂到视频")]);
      list.append(
        el("article", { class: "row" }, [
          el("div", { class: "meta", text: `${TYPE_LABEL[item.type] || item.type} · ${STATUS_LABEL[item.status] || item.status}` }),
          el("h2", { text: item.key }),
          el("p", { text: item.zh || "待补中文" }),
          first ? el("p", { class: "sentence", text: first.sentence }) : null,
          first ? el("p", { class: "meta", text: formatTime(first.start) }) : null,
          linkRow,
        ])
      );
    });
  }

  videoFilter.addEventListener("change", renderList);
  showCandidates.addEventListener("change", renderList);

  const strip = el(
    "div",
    { class: "video-strip" },
    (data.videos || []).map((video) =>
      el("a", {
        class: "video-link",
        href: `${video.slug}.html`,
        text: `${video.title} · ${video.source_label} · 在背 ${video.learning} · 待决定 ${video.candidates}`,
      })
    )
  );

  app.append(
    el("div", { class: "wrap" }, [
      el("header", { class: "top" }, [
        el("div", {}, [
          el("h1", { text: "全部视频" }),
          el("p", { class: "sub", text: "在背的是你点过「加入背诵」的词和短语。待决定的还在各视频的单词卡里。" }),
        ]),
      ]),
      el("div", { class: "stats" }, [
        el("span", { class: "stat", text: `视频 ${stats.videos || 0}` }),
        el("span", { class: "stat", text: `在背单词 ${stats.learning_words || 0}` }),
        el("span", { class: "stat", text: `在背短语 ${stats.learning_phrases || 0}` }),
        el("span", { class: "stat", text: `待决定 ${stats.candidates || 0}` }),
      ]),
      strip,
      el("div", { class: "filters" }, [
        videoFilter,
        el("label", { class: "check" }, [showCandidates, document.createTextNode("显示待决定")]),
        countLine,
      ]),
      list,
      el("p", { class: "help", text: "卡片上的标记会下载成 marks.json。把那个文件交给我，或者说「导入标记」。" }),
    ])
  );
  renderList();
}

function renderStudy() {
  const video = data.video;
  const items = data.items || [];
  const storeKey = `video-vocab-marks:${video.slug}`;
  let marks = {};
  try {
    marks = JSON.parse(localStorage.getItem(storeKey) || "{}");
  } catch (error) {
    marks = {};
  }
  let index = 0;
  const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
  const found = items.findIndex((item) => item.key === hash);
  if (found >= 0) index = found;

  const progress = el("p", { class: "progress" });
  const stage = el("div", { class: "stage" });
  const note = el("p", { class: "help" });
  let cardEl = null;

  function saveMarks() {
    localStorage.setItem(storeKey, JSON.stringify(marks));
    const count = Object.keys(marks).length;
    note.textContent = count ? `本页有 ${count} 条标记还没写入账本。` : "点按钮只改这一页，导出后才会写入账本。";
  }

  function effective(item) {
    return marks[item.key] || item.status;
  }

  function renderCard(keepFlip) {
    progress.textContent = items.length ? `${index + 1} / ${items.length}` : "0 / 0";
    if (!items.length) {
      stage.replaceChildren(el("p", { class: "empty", text: "这支视频没有需要决定的生词或短语。" }));
      cardEl = null;
      return;
    }
    const item = items[index];
    const wasFlipped = Boolean(keepFlip && cardEl && cardEl.classList.contains("is-flipped"));
    const front = el("div", { class: "face front" }, [
      el("div", { class: "eyebrow", text: TYPE_LABEL[item.type] || item.type }),
      el("h2", { class: "word", text: item.key }),
      hintFor(item) ? el("p", { class: "hint", text: hintFor(item) }) : null,
      el("p", { class: "hint", text: "点击卡片看中文" }),
    ]);
    const back = el("div", { class: "face back" }, [
      el("p", { class: "zh", text: item.zh || "待补中文" }),
      el("p", { class: "sentence", text: item.sentence }),
      el("p", { class: "meta", text: `${formatTime(item.start)}–${formatTime(item.end)}` }),
    ]);
    cardEl = el("article", { class: wasFlipped ? "card is-flipped" : "card" }, [front, back]);
    cardEl.addEventListener("click", () => cardEl.classList.toggle("is-flipped"));
    const status = effective(item);
    const markButton = (label, value, className) =>
      el("button", {
        type: "button",
        class: `${className || ""} ${status === value ? "is-on" : ""}`.trim(),
        text: label,
        onClick: () => {
          marks[item.key] = value;
          saveMarks();
          renderCard(true);
        },
      });
    stage.replaceChildren(
      cardEl,
      el("div", { class: "actions" }, [
        el("button", { type: "button", text: "上一张", disabled: index === 0, onClick: () => step(-1) }),
        el("button", { type: "button", text: "下一张", disabled: index === items.length - 1, onClick: () => step(1) }),
        markButton("认识 (1)", "known"),
        markButton("加入背诵 (2)", "learning", "primary"),
        markButton("忽略 (3)", "ignored"),
      ])
    );
  }

  function step(delta) {
    index = Math.min(items.length - 1, Math.max(0, index + delta));
    renderCard(false);
  }

  function exportMarks() {
    const payload = { marks: Object.entries(marks).map(([key, status]) => ({ key, status })) };
    if (!payload.marks.length) {
      note.textContent = "还没有新的标记。";
      return;
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: "marks.json" });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    note.textContent = "已下载 marks.json。把文件交给我，或者说「导入标记」。";
  }

  function learnAll() {
    items.forEach((item) => {
      if (item.status !== "learning") marks[item.key] = "learning";
    });
    saveMarks();
    renderCard(true);
  }

  document.addEventListener("keydown", (event) => {
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (!items.length) return;
    if ((event.key === " " || event.key === "Enter") && tag !== "button" && tag !== "a") {
      event.preventDefault();
      if (cardEl) cardEl.classList.toggle("is-flipped");
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1);
    } else if (event.key === "1") {
      marks[items[index].key] = "known";
      saveMarks();
      renderCard(true);
    } else if (event.key === "2") {
      marks[items[index].key] = "learning";
      saveMarks();
      renderCard(true);
    } else if (event.key === "3") {
      marks[items[index].key] = "ignored";
      saveMarks();
      renderCard(true);
    }
  });

  const bannerText = video.source === "asr"
    ? (video.warning || "这些句子来自机器听写。如果不对，请点忽略。")
    : video.warning;

  app.append(
    el("div", { class: "wrap" }, [
      el("header", { class: "top" }, [
        el("div", {}, [
          el("p", { class: "eyebrow", text: video.source_label || "" }),
          el("h1", { text: video.title }),
          video.path ? el("p", { class: "path", text: video.path }) : null,
        ]),
        el("a", { href: "index.html", text: "全部视频" }),
      ]),
      bannerText ? el("div", { class: "banner", text: bannerText }) : null,
      progress,
      stage,
      el("div", { class: "actions" }, [
        el("button", { type: "button", class: "primary", text: "全部加入背诵", onClick: learnAll }),
        el("button", { type: "button", text: "导出标记", onClick: exportMarks }),
      ]),
      note,
      el("p", { class: "help", text: "空格翻转，左右方向键换卡。认识会写入已知词表，以后不再推荐。忽略也不会再推荐。加入背诵会计入这支视频和总账。" }),
    ])
  );
  saveMarks();
  renderCard();
}

if (data.mode === "study") renderStudy();
else renderIndex();
