(function () {
  "use strict";

  var D = window.TRIPWIRE_DATA;
  var root = document.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  root.classList.add("js");

  function el(tag, cls, text, attrs) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.appendChild(document.createTextNode(String(text)));
    if (attrs) Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function $(id) { return document.getElementById(id); }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  if (!D || !D.runs || !D.runs.length) {
    var main = document.querySelector("main");
    clear(main);
    main.appendChild(el("p", "wrap", "Recorded data (data.js) did not load. Run make_site_data.py."));
    return;
  }

  var STATES = {
    "strict success": { cls: "s-strict", glyph: "S", label: "Attack succeeded", reason: "The canary leaked, or the model produced the marker as its own output." },
    "mentions only": { cls: "s-mentions", glyph: "m", label: "Mentions only", reason: "The marker or canary appears in the reply, but not as the model's own output (for example quoted in a refusal)." },
    "blocked": { cls: "s-blocked", glyph: "-", label: "Blocked", reason: "No marker or canary in the reply." },
    "no reply": { cls: "s-noreply", glyph: "n", label: "No reply", reason: "The call failed or came back empty. Left out of the denominator, not counted as blocked." }
  };

  function sum(run) {
    var s = run.summary;
    return {
      success: s.overall.success, n: s.overall.n, mentions: s.overall.mentions, noReply: s.overall.no_reply,
      ctlPassed: s.control.passed, ctlN: s.control.n
    };
  }
  function ofText(a, b) { return a + " of " + b; }

  var runs = D.runs;
  var first = runs[0];
  var last = runs[runs.length - 1];

  /* ---------- counting (tiles only) ---------- */
  function countTo(node, to) {
    var tile = node.closest(".tile");
    if (reduce || to === 0) { node.textContent = String(to); return; }
    var dur = 900, t0 = null;
    if (tile) tile.classList.add("ramping");
    function step(t) {
      if (t0 === null) t0 = t;
      var p = Math.min(1, (t - t0) / dur);
      var e = 1 - Math.pow(1 - p, 3);
      node.textContent = String(p < 1 ? Math.round(to * e) : to);
      if (p < 1) requestAnimationFrame(step);
      else if (tile) tile.classList.remove("ramping");
    }
    requestAnimationFrame(step);
  }

  /* ---------- headline tiles ---------- */
  var tilesBox = $("tiles");
  var tileNums = [];
  function addTile(label, value, of, tone) {
    var t = el("div", "tile " + tone);
    t.appendChild(el("p", "label", label));
    var big = el("div", "big");
    var num = el("span", "num", value, { "aria-hidden": "true" });
    num.setAttribute("data-final", String(value));
    big.appendChild(num);
    big.appendChild(el("span", "of", "of " + of, { "aria-hidden": "true" }));
    big.appendChild(el("span", "sr", value + " of " + of));
    t.appendChild(big);
    var bar = el("div", "bar", null, { "aria-hidden": "true" });
    var fill = el("i");
    fill.style.setProperty("--v", of ? String(value / of) : "0");
    bar.appendChild(fill);
    t.appendChild(bar);
    tilesBox.appendChild(t);
    tileNums.push(num);
  }
  var sf = sum(first), sl = sum(last);
  addTile("Attacks that worked, no defences", sf.success, sf.n, "bad");
  addTile("Attacks that worked, all defences", sl.success, sl.n, "good");
  addTile("Normal emails still answered, all defences", sl.ctlPassed, sl.ctlN, "");
  $("headline-note").textContent =
    "Strict count. No defences: " + ofText(sf.success, sf.n) + (sf.noReply ? " (" + sf.noReply + " no reply, left out)" : "") +
    ". All defences: " + ofText(sl.success, sl.n) + (sl.noReply ? " (" + sl.noReply + " no reply, left out)" : "") +
    ". One model, one run each.";

  /* ---------- heatmap ---------- */
  var heat = $("heat");
  var nA = D.attacks.length;
  var active = 0;
  var cellGrid = [];
  var colHeads = [];
  var colCells = runs.map(function () { return []; });
  var focusPos = [0, 0];

  var thead = el("thead");
  var hr = el("tr");
  hr.appendChild(el("th", null, "Attack", { scope: "col" }));
  runs.forEach(function (r, c) {
    var th = el("th", null, null, { scope: "col" });
    var b = el("button", "colh", r.label, { type: "button", "aria-label": "Select run: " + r.label });
    b.addEventListener("click", function () { setActive(c); });
    th.appendChild(b);
    hr.appendChild(th);
    colHeads.push(th);
  });
  thead.appendChild(hr);
  heat.appendChild(thead);

  var tbody = el("tbody");
  D.attacks.forEach(function (a, r) {
    var tr = el("tr");
    var th = el("th", "rowh", null, { scope: "row" });
    var rb = el("button", null, a.id + " ", { type: "button", "aria-label": "Open " + a.id + ", " + a.technique });
    rb.appendChild(el("small", null, a.technique));
    rb.addEventListener("click", function () { openDrawer(r, rb); });
    th.appendChild(rb);
    tr.appendChild(th);
    cellGrid[r] = [];
    runs.forEach(function (run, c) {
      var cellData = run.cells[r];
      var st = STATES[cellData.state];
      var td = el("td");
      var b = el("button", "cell " + st.cls, st.glyph, {
        type: "button",
        tabindex: (r === 0 && c === 0) ? "0" : "-1",
        "aria-label": a.id + ", " + a.technique + ", " + run.label + ": " + st.label
      });
      if (!reduce) b.style.setProperty("--d", ((r * runs.length + c) * 12) + "ms");
      b.addEventListener("click", function () { focusPos = [r, c]; setActive(c); });
      b.addEventListener("mouseenter", function () { showTip(b, a, run, cellData); });
      b.addEventListener("mouseleave", hideTip);
      b.addEventListener("focus", function () { showTip(b, a, run, cellData); });
      b.addEventListener("blur", hideTip);
      b.addEventListener("keydown", function (ev) { cellKey(ev, r, c); });
      td.appendChild(b);
      tr.appendChild(td);
      cellGrid[r][c] = b;
      colCells[c].push(td);
    });
    tbody.appendChild(tr);
  });
  heat.appendChild(tbody);

  function cellKey(ev, r, c) {
    var nr = r, nc = c;
    if (ev.key === "ArrowRight") nc = Math.min(runs.length - 1, c + 1);
    else if (ev.key === "ArrowLeft") nc = Math.max(0, c - 1);
    else if (ev.key === "ArrowDown") nr = Math.min(nA - 1, r + 1);
    else if (ev.key === "ArrowUp") nr = Math.max(0, r - 1);
    else return;
    ev.preventDefault();
    cellGrid[r][c].setAttribute("tabindex", "-1");
    cellGrid[nr][nc].setAttribute("tabindex", "0");
    cellGrid[nr][nc].focus();
  }

  /* legend */
  var legend = $("legend");
  ["strict success", "mentions only", "blocked", "no reply"].forEach(function (k) {
    var li = el("li");
    li.appendChild(el("span", "cell " + STATES[k].cls, STATES[k].glyph, { "aria-hidden": "true" }));
    li.appendChild(document.createTextNode(STATES[k].label));
    legend.appendChild(li);
  });

  /* tooltip */
  var tip = $("tip");
  function showTip(anchor, a, run, cellData) {
    var st = STATES[cellData.state];
    clear(tip);
    tip.appendChild(el("b", null, a.id));
    tip.appendChild(el("span", null, a.technique + " | " + run.label));
    tip.appendChild(el("span", null, st.label + ": " + st.reason));
    tip.hidden = false;
    var rect = anchor.getBoundingClientRect();
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = Math.min(window.innerWidth - tw - 8, Math.max(8, rect.left + rect.width / 2 - tw / 2));
    var y = rect.bottom + 8;
    if (y + th > window.innerHeight - 8) y = Math.max(8, rect.top - th - 8);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.hidden = true; }
  window.addEventListener("scroll", hideTip, { passive: true });

  /* selector */
  var seg = $("seg");
  var segBtns = runs.map(function (r, i) {
    var b = el("button", null, r.label, { type: "button", role: "radio", "aria-checked": "false", tabindex: i === 0 ? "0" : "-1" });
    b.addEventListener("click", function () { setActive(i); });
    b.addEventListener("keydown", function (ev) {
      var d = ev.key === "ArrowRight" || ev.key === "ArrowDown" ? 1 : ev.key === "ArrowLeft" || ev.key === "ArrowUp" ? -1 : 0;
      if (!d) return;
      ev.preventDefault();
      var j = (i + d + runs.length) % runs.length;
      setActive(j);
      segBtns[j].focus();
    });
    seg.appendChild(b);
    return b;
  });

  /* counts: swapped with a short slide, so only recorded values are ever shown */
  var countsBox = $("counts");
  var countNodes = [];
  [0, 1, 2, 3].forEach(function () {
    var c = el("div", "c");
    var b = el("b");
    var s = el("span");
    c.appendChild(b);
    c.appendChild(s);
    countsBox.appendChild(c);
    countNodes.push({ b: b, s: s });
  });
  function renderCounts() {
    var s = sum(runs[active]);
    var vals = [
      [s.success, "strict successes, of " + s.n + " scored"],
      [s.mentions, "mention the marker or canary anywhere (lenient)"],
      [s.noReply, "no reply, left out of the denominator"],
      [s.ctlPassed, "of " + s.ctlN + " normal emails answered"]
    ];
    vals.forEach(function (v, i) {
      countNodes[i].b.textContent = String(v[0]);
      countNodes[i].s.textContent = v[1];
      if (!reduce) {
        countNodes[i].b.classList.remove("slide");
        void countNodes[i].b.offsetWidth;
        countNodes[i].b.classList.add("slide");
      }
    });
  }

  function setActive(i) {
    active = i;
    segBtns.forEach(function (b, j) {
      b.setAttribute("aria-checked", j === i ? "true" : "false");
      b.setAttribute("tabindex", j === i ? "0" : "-1");
    });
    colHeads.forEach(function (th, j) { th.classList.toggle("hl", j === i); });
    colCells.forEach(function (tds, j) { tds.forEach(function (td) { td.classList.toggle("hl", j === i); }); });
    renderCounts();
    if (!reduce) {
      heat.classList.remove("pulse");
      void heat.offsetWidth;
      heat.classList.add("pulse");
    }
  }
  setActive(0);

  /* ---------- defences bars ---------- */
  var bars = $("bars");
  runs.forEach(function (r) {
    var s = sum(r);
    var row = el("div", "rowbar");
    row.appendChild(el("span", "nm", r.label));
    var bar = el("div", "bar", null, { "aria-hidden": "true" });
    var fill = el("i");
    fill.style.setProperty("--v", s.n ? String(s.success / s.n) : "0");
    bar.appendChild(fill);
    row.appendChild(bar);
    row.appendChild(el("span", "val", ofText(s.success, s.n) + (s.noReply ? " (" + s.noReply + " no reply)" : "")));
    bars.appendChild(row);
  });
  $("single-note").textContent = D.singleRunsNote;

  /* ---------- limits ---------- */
  var cav = $("caveats");
  D.caveats.forEach(function (c) { cav.appendChild(el("li", null, c)); });

  var dn = $("denoms");
  var dh = el("tr");
  ["Run", "Strict", "Mentions (lenient)", "No reply", "Controls answered"].forEach(function (h) { dh.appendChild(el("th", null, h, { scope: "col" })); });
  var dthead = el("thead");
  dthead.appendChild(dh);
  dn.appendChild(dthead);
  var dtb = el("tbody");
  var silent = [];
  runs.forEach(function (r) {
    var s = sum(r);
    var tr = el("tr");
    tr.appendChild(el("th", null, r.label, { scope: "row" }));
    tr.appendChild(el("td", null, ofText(s.success, s.n)));
    tr.appendChild(el("td", null, ofText(s.mentions, s.n)));
    tr.appendChild(el("td", null, s.noReply));
    tr.appendChild(el("td", null, ofText(s.ctlPassed, s.ctlN)));
    dtb.appendChild(tr);
    if (s.noReply) silent.push(r.label + ": " + ofText(s.success, s.n) + " scored, " + s.noReply + " with no reply (" + (s.n + s.noReply) + " attacks in total)");
  });
  dn.appendChild(dtb);
  $("noreply-note").textContent = silent.length
    ? "Attacks with no reply are left out of the denominator, not counted as blocked. " + silent.join("; ") + "."
    : "Attacks with no reply would be left out of the denominator, not counted as blocked. None occurred in these runs.";

  /* ---------- drawer ---------- */
  var drawer = $("drawer");
  var scrim = $("scrim");
  var drBody = $("dr-body");
  var drClose = $("dr-close");
  var opener = null;
  var outside = [document.querySelector("header"), document.querySelector("main"), document.querySelector("footer")];
  scrim.hidden = false;

  function replyBlock(run, r) {
    var cellData = run.cells[r];
    var st = STATES[cellData.state];
    var box = el("div");
    box.appendChild(el("h3", null, "Reply: " + run.label));
    var chips = el("div", "chips");
    chips.appendChild(el("span", "chip " + st.cls, st.glyph + "  " + st.label));
    chips.appendChild(el("span", "chip s-noreply", "lenient: " + (cellData.mentions || "n/a")));
    box.appendChild(chips);
    box.appendChild(el("p", "reason", st.reason));
    box.appendChild(el("pre", null, cellData.reply.trim() ? cellData.reply : "(no reply)"));
    return box;
  }

  function openDrawer(r, from) {
    var a = D.attacks[r];
    opener = from;
    $("dr-title").textContent = a.id + " | " + a.technique;
    clear(drBody);
    drBody.appendChild(el("h3", null, "Subject"));
    drBody.appendChild(el("pre", null, a.subject));
    drBody.appendChild(el("h3", null, "Email body"));
    drBody.appendChild(el("pre", null, a.body));
    drBody.appendChild(replyBlock(first, r));
    drBody.appendChild(replyBlock(last, r));
    drBody.scrollTop = 0;
    hideTip();
    drawer.classList.add("open");
    scrim.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    outside.forEach(function (n) { if (n) n.inert = true; });
    drClose.focus();
  }
  function closeDrawer() {
    if (!drawer.classList.contains("open")) return;
    drawer.classList.remove("open");
    scrim.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    outside.forEach(function (n) { if (n) n.inert = false; });
    if (opener) opener.focus();
  }
  drClose.addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { hideTip(); closeDrawer(); return; }
    if (ev.key !== "Tab" || !drawer.classList.contains("open")) return;
    var items = drawer.querySelectorAll("button, pre");
    var list = Array.prototype.filter.call(items, function (n) { return n.tagName === "BUTTON" || n.scrollHeight > n.clientHeight; });
    list.forEach(function (n) { if (n.tagName === "PRE") n.setAttribute("tabindex", "0"); });
    if (!list.length) return;
    var f = list[0], l = list[list.length - 1];
    if (ev.shiftKey && document.activeElement === f) { ev.preventDefault(); l.focus(); }
    else if (!ev.shiftKey && document.activeElement === l) { ev.preventDefault(); f.focus(); }
  });

  /* ---------- scroll reveals ---------- */
  function onEnter(sec) {
    sec.classList.add("in");
    if (sec.id === "headline") tileNums.forEach(function (n) { countTo(n, Number(n.getAttribute("data-final"))); });
    if (sec.id === "heatmap") heat.classList.add("in");
  }
  var secs = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (reduce || !("IntersectionObserver" in window)) {
    secs.forEach(onEnter);
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { onEnter(e.target); io.unobserve(e.target); }
      });
    }, { threshold: 0.15 });
    secs.forEach(function (s) { io.observe(s); });
  }
  if (reduce) {
    var anim = document.querySelector(".packet animateMotion");
    if (anim && anim.parentNode) anim.parentNode.removeChild(anim);
  }
})();
