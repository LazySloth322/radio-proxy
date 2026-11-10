/* private-radio-proxy frontend - vanilla JS, no frameworks, no CDNs. */
(function () {
  "use strict";

  // The page is served at <secret>/ so BASE is the secret path itself.
  var BASE = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";

  // Fallback auth: some browsers/contexts may reject the cookie, so keep the
  // query token (if present in the opening URL) and attach it to every request.
  var QTOKEN = new URLSearchParams(location.search).get("token") || "";

  function authUrl(rel) {
    var url = rel;
    if (QTOKEN) url += (url.indexOf("?") === -1 ? "?" : "&") + "token=" + encodeURIComponent(QTOKEN);
    return url;
  }

  var POLL_MS = 15000;

  var state = {
    cards: {},       // id -> card element
    stations: [],    // last known list
    currentId: null, // station currently loaded in the audio element
    lastId: null,    // last selected station (so the bar's play button can restart it)
  };

  // NOTE: the Audio element is created lazily per play session (spawnAudio);
  // a fresh element behaves exactly like the first play after a page load,
  // which is the most reliable path across browsers.
  var audio = null;

  // ------------------------------------------------------------- elements
  var grid = document.getElementById("stations");
  var banner = document.getElementById("error-banner");
  var conn = document.getElementById("conn-status");
  var playerStation = document.getElementById("player-station");
  var playerTrack = document.getElementById("player-track");
  var playerToggle = document.getElementById("player-toggle");
  var volumeSlider = document.getElementById("volume");
  var canvas = document.getElementById("spectrum");
  var themeToggle = document.getElementById("theme-toggle");
  var historyToggle = document.getElementById("history-toggle");
  var historyPanel = document.getElementById("history-panel");

  function showBanner(msg) {
    banner.textContent = msg;
    banner.classList.remove("hidden");
  }
  function hideBanner() { banner.classList.add("hidden"); }

  function fetchJSON(url) {
    return fetch(url, { cache: "no-store", credentials: "same-origin" }).then(function (r) {
      if (r.status === 429) throw new Error("Too many requests - slow down for a minute.");
      if (!r.ok) throw new Error("Request failed (" + r.status + "). Check your access link.");
      return r.json();
    });
  }

  function stationIn(list, id) {
    if (!id) return null;
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  // ---------------------------------------------------------------- theme

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function refreshThemeButton() {
    var dark = currentTheme() === "dark";
    themeToggle.textContent = dark ? "\u263D" : "\u263C"; // moon / sun
    themeToggle.title = dark ? "Switch to light theme" : "Switch to dark theme";
    viz.colorDirty = true;
  }

  themeToggle.addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    if (window.RPTheme) window.RPTheme.set(next);
    else document.documentElement.setAttribute("data-theme", next);
    refreshThemeButton();
  });

  // --------------------------------------------------------------- volume

  var volume = 0.8;
  try {
    var savedVolume = parseFloat(localStorage.getItem("rp_volume"));
    if (!isNaN(savedVolume) && savedVolume >= 0 && savedVolume <= 1) volume = savedVolume;
  } catch (e) { /* storage blocked - keep default */ }
  volumeSlider.value = Math.round(volume * 100);
  volumeSlider.addEventListener("input", function () {
    volume = volumeSlider.value / 100;
    if (audio) audio.volume = volume;
    try { localStorage.setItem("rp_volume", String(volume)); } catch (e) { /* ignore */ }
  });

  // ------------------------------------------- session history (recent tracks)
  // Captured CLIENT-SIDE from the 15 s metadata poll, so it holds only tracks
  // heard on THIS page session, is bound to the token-authenticated tab
  // (nothing is sent to or stored on the server, nothing persists a reload),
  // and is wiped whenever the listener switches station. The array is capped
  // at player.history_limit (config/app.json, delivered via /api/stations),
  // so memory stays constant no matter how long the session runs.

  var history = {
    stationId: null, // station the list belongs to; list resets when it changes
    items: [],       // "Artist - Title" strings, newest first
    limit: 30,       // player.history_limit (fallback until the first poll)
    visible: 15,     // player.history_visible_items (rows shown before scroll)
    lastKey: "",     // dedupe guard vs. the previous poll
  };

  function applyPlayerConfig(p) {
    if (!p || typeof p !== "object") return;
    var n = parseInt(p.history_limit, 10);
    if (!isNaN(n) && n >= 1) {
      history.limit = Math.min(n, 500);
      if (history.items.length > history.limit) {
        history.items.length = history.limit;
        renderHistory();
      }
    }
    var v = parseInt(p.history_visible_items, 10);
    if (!isNaN(v) && v >= 1) {
      history.visible = Math.min(v, 100);
      historyPanel.style.setProperty("--hist-visible", String(history.visible));
    }
  }

  function resetHistory(stationId) {
    history.stationId = stationId || null;
    history.items.length = 0;
    history.lastKey = "";
    renderHistory();
  }

  function captureTrack(stations) {
    var cur = stationIn(stations, state.currentId);
    var d = cur && cur.now_playing ? String(cur.now_playing.display || "").trim() : "";
    if (!d || d === "Unknown") return;  // station provides no metadata right now
    // Safety net: if the station changed between polls, start a fresh list.
    if (history.stationId !== state.currentId) resetHistory(state.currentId);
    if (d === history.lastKey) return;
    history.lastKey = d;
    history.items.unshift(d);
    if (history.items.length > history.limit) history.items.length = history.limit;
    renderHistory();
  }

  function renderHistory() {
    historyPanel.textContent = "";  // rebuild is cheap: at most history.limit rows
    if (!history.items.length) {
      var li = document.createElement("li");
      li.className = "history-empty";
      li.textContent = "No tracks captured yet on this session.";
      historyPanel.appendChild(li);
      return;
    }
    for (var i = 0; i < history.items.length; i++) {
      var li = document.createElement("li");
      li.textContent = history.items[i];
      if (i === 0) li.className = "now";
      historyPanel.appendChild(li);
    }
  }

  historyToggle.addEventListener("click", function () {
    var open = historyPanel.classList.contains("hidden");
    historyPanel.classList.toggle("hidden", !open);
    historyToggle.classList.toggle("open", open);
    historyToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) renderHistory();
  });

  function spawnAudio() {
    if (audio) {
      var old = audio;
      try { old.pause(); } catch (e) { /* ignore */ }
      old.removeAttribute("src");
      try { old.load(); } catch (e) { /* ignore */ }  // abort the old download
    }
    var el = new Audio();
    el.preload = "none";
    el.volume = volume;
    // Event guards: a stale element (e.g. the previous session aborting its
    // download) fires its events AFTER a new session has already started.
    // Only the element that is still the current one may act.
    el.addEventListener("error", function () {
      if (audio !== el || !state.currentId) return;
      var code = el.error && el.error.code;
      showBanner(code === 4
        ? "Stream failed or station offline."
        : "Audio error (code " + (code || "?") + ").");
      stopPlaybackOnly();
      update(state.stations);
    });
    el.addEventListener("playing", function () {
      if (audio === el) update(state.stations);
    });
    el.addEventListener("waiting", function () {
      if (audio === el) update(state.stations);
    });
    el.addEventListener("pause", function () {
      if (audio === el) update(state.stations);
    });
    audio = el;
    return el;
  }

  // ------------------------------------------------------ spectrum (Web Audio)
  // The audio element streams from this very server (same origin), so wiring
  // it through an AnalyserNode does not hit CORS tainting. If Web Audio is
  // unavailable for any reason, playback continues normally without visuals.

  var viz = {
    ok: true,       // becomes false if Web Audio cannot be used
    ctx: null,      // AudioContext (created lazily on first play = user gesture)
    analyser: null,
    src: null,      // MediaElementSourceNode for the current audio element
    data: null,     // reusable frequency bin buffer
    raf: 0,         // requestAnimationFrame handle
    color: "",      // cached bar color (re-read on theme change)
    colorDirty: true,
    bars: 28,       // number of bars
  };

  function accentColor() {
    var cs = getComputedStyle(document.documentElement);
    var c = (cs.getPropertyValue("--accent") || "").trim();
    return c || "#4da3ff";
  }

  function vizConnect() {
    /* Wire the current audio element into the graph. Called once per play
     * session because spawnAudio() creates a new element each time. */
    if (!viz.ok || !audio) return;
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) { viz.ok = false; return; }
      if (!viz.ctx) {
        viz.ctx = new AC();
        viz.analyser = viz.ctx.createAnalyser();
        viz.analyser.fftSize = 256;              // 128 bins; plenty for bars
        viz.analyser.smoothingTimeConstant = 0.82;
        viz.analyser.connect(viz.ctx.destination);  // keep the sound audible
        viz.data = new Uint8Array(viz.analyser.frequencyBinCount);
      }
      if (viz.src) { try { viz.src.disconnect(); } catch (e) { /* ignore */ } }
      viz.src = viz.ctx.createMediaElementSource(audio);
      viz.src.connect(viz.analyser);
    } catch (e) {
      viz.ok = false; viz.ctx = null; viz.analyser = null;
      viz.src = null; viz.data = null;
    }
  }

  function resizeCanvas() {
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    var h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }

  function vizIdle() {
    resizeCanvas();
    var g = canvas.getContext("2d");
    if (!g) return;
    g.clearRect(0, 0, canvas.width, canvas.height);
    g.fillStyle = (getComputedStyle(document.documentElement)
      .getPropertyValue("--muted") || "#8a93a5").trim();
    g.globalAlpha = 0.35;
    g.fillRect(0, canvas.height - Math.max(2, Math.round(canvas.height * 0.05)),
               canvas.width, Math.max(2, Math.round(canvas.height * 0.02)));
    g.globalAlpha = 1;
  }

  function vizDraw() {
    viz.raf = 0;
    var playing = audio && state.currentId && !audio.paused && !audio.ended;
    if (!playing) { vizIdle(); return; }

    resizeCanvas();
    var g = canvas.getContext("2d");
    if (!g) return;
    var w = canvas.width, h = canvas.height;
    g.clearRect(0, 0, w, h);

    if (viz.colorDirty || !viz.color) { viz.color = accentColor(); viz.colorDirty = false; }
    g.fillStyle = viz.color;

    if (viz.analyser && viz.data) {
      viz.analyser.getByteFrequencyData(viz.data);
      var gap = Math.max(1, Math.round(w * 0.004));
      var bw = Math.floor((w - gap * (viz.bars - 1)) / viz.bars);
      if (bw < 1) bw = 1;
      var usable = Math.floor(viz.data.length * 0.7);  // music lives in the low bins
      for (var i = 0; i < viz.bars; i++) {
        var from = Math.floor(i * usable / viz.bars);
        var to = Math.max(from + 1, Math.floor((i + 1) * usable / viz.bars));
        var sum = 0;
        for (var j = from; j < to; j++) sum += viz.data[j];
        var v = sum / (to - from) / 255;
        var bh = Math.max(2, Math.round(v * h * 0.92));
        g.globalAlpha = 0.35 + 0.65 * v;
        g.fillRect(i * (bw + gap), h - bh, bw, bh);
      }
      g.globalAlpha = 1;
    }

    viz.raf = requestAnimationFrame(vizDraw);
  }

  function vizStart() {
    if (viz.ctx && viz.ctx.resume) {
      try { viz.ctx.resume(); } catch (e) { /* ignore */ }
    }
    if (!viz.raf) viz.raf = requestAnimationFrame(vizDraw);
  }

  function vizStop() {
    if (viz.raf) { cancelAnimationFrame(viz.raf); viz.raf = 0; }
    // NOTE: the AudioContext is deliberately NOT suspended here. Suspending
    // back-pressures the media element and a fast stop->play can abort the
    // new play() call; an idle context without a rAF loop costs nothing.
    vizIdle();
  }

  // ------------------------------------------------------------- rendering
  // Track names live ONLY in the bottom player bar (per owner's request),
  // cards stay clean: cover, status dot, name, codec + play button.

  function buildCard(st) {
    var card = document.createElement("article");
    card.className = "card";
    card.dataset.id = st.id;

    var coverWrap = document.createElement("div");
    coverWrap.className = "cover-wrap";

    var img = document.createElement("img");
    img.className = "cover";
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.src = authUrl(BASE + "radio/" + st.id + "/cover");
    img.addEventListener("error", function () {
      img.src = BASE + "assets/fallback-cover.svg";
    });

    var dot = document.createElement("span");
    dot.className = "status-dot status-unknown";
    dot.title = "unknown";

    coverWrap.appendChild(img);
    coverWrap.appendChild(dot);

    var name = document.createElement("h2");
    name.textContent = st.name;

    var row = document.createElement("div");
    row.className = "meta-row";

    var badge = document.createElement("span");
    badge.className = "codec-badge";
    badge.textContent = (st.codec || "").toUpperCase() +
      (st.bitrate_kbps ? " - " + st.bitrate_kbps + " kbps" : "");

    var btn = document.createElement("button");
    btn.className = "btn-play";
    btn.type = "button";
    btn.textContent = "Play";
    btn.addEventListener("click", debounceToggle(function () {
      if (state.currentId === st.id) { stop(); } else { play(st.id); }
    }));

    row.appendChild(badge);
    row.appendChild(btn);

    card.appendChild(coverWrap);
    card.appendChild(name);
    card.appendChild(row);
    return card;
  }

  function render(stations) {
    var seen = {};
    stations.forEach(function (st) {
      seen[st.id] = true;
      if (!state.cards[st.id]) {
        var card = buildCard(st);
        state.cards[st.id] = card;
        grid.appendChild(card);
      }
    });
    Object.keys(state.cards).forEach(function (id) {
      if (!seen[id]) {
        grid.removeChild(state.cards[id]);
        delete state.cards[id];
        if (state.currentId === id) stop();
      }
    });
    state.stations = stations;
    captureTrack(stations);
    update(stations);
  }

  function update(stations) {
    var now = new Date();
    conn.textContent = "updated " +
      ("0" + now.getHours()).slice(-2) + ":" + ("0" + now.getMinutes()).slice(-2);

    stations.forEach(function (st) {
      var card = state.cards[st.id];
      if (!card) return;
      var dot = card.querySelector(".status-dot");
      dot.className = "status-dot status-" + (st.status || "unknown");
      dot.title = st.status || "unknown";
      var isLive = state.currentId === st.id;
      var playing = isLive && audio && !audio.paused && !audio.ended;
      var btn = card.querySelector(".btn-play");
      btn.textContent = playing ? "Stop" : "Play";
      btn.classList.toggle("playing", playing);
      card.classList.toggle("playing", isLive);
    });

    // ---- bottom player bar ----
    var current = stationIn(stations, state.currentId);
    var playingNow = !!current && audio && !audio.paused && !audio.ended;
    var display = current && current.now_playing && current.now_playing.display;

    playerStation.textContent = current ? current.name : "Nothing playing";
    if (current && display && display !== "Unknown") {
      playerTrack.textContent = display;
      playerTrack.classList.add("live");
    } else if (current) {
      playerTrack.textContent = "Streaming... (no metadata)";
      playerTrack.classList.remove("live");
    } else {
      playerTrack.textContent = "Pick a station to start listening";
      playerTrack.classList.remove("live");
    }

    playerToggle.disabled = !playingNow && !state.lastId;
    playerToggle.textContent = playingNow ? "\u25A0" : "\u25B6"; // stop / play
    playerToggle.title = playingNow ? "Stop" : "Play";
    playerToggle.classList.toggle("playing", playingNow);

    if (playingNow) vizStart(); else vizStop();
  }

  // -------------------------------------------------------------- playback

  function play(id) {
    hideBanner();
    var prev = state.currentId;
    if (prev && prev !== id) stopPlaybackOnly();
    if (history.stationId !== id) resetHistory(id);  // new station = fresh list
    state.currentId = id;
    state.lastId = id;

    spawnAudio();
    audio.src = authUrl(BASE + "radio/" + id + "/stream");
    vizConnect();

    var start = function (isRetry) {
      var p = audio.play();
      if (p && p.catch) {
        p.catch(function (e) {
          // A fast AbortError right after start is a browser teardown race
          // (the previous session being released), not an offline station -
          // retry once on the same element before reporting failure.
          if (e && e.name === "AbortError" && !isRetry && state.currentId === id) {
            setTimeout(function () {
              if (state.currentId === id && audio && audio.paused) start(true);
            }, 350);
            return;
          }
          showBanner("Playback failed. The station may be offline - try again shortly.");
          stopPlaybackOnly();
          update(state.stations);
        });
      }
    };

    // Resume first if needed: a suspended context back-pressures the media
    // element and can abort play(), so make the ordering deterministic.
    if (viz.ctx && viz.ctx.state === "suspended" && viz.ctx.resume) {
      viz.ctx.resume().then(function () { start(false); }, function () { start(false); });
    } else {
      start(false);
    }
    vizStart();
    update(state.stations);
  }

  function stopPlaybackOnly() {
    if (!audio) return;
    try { audio.pause(); } catch (e) { /* ignore */ }
    audio.removeAttribute("src");
    try { audio.load(); } catch (e) { /* ignore */ }  // fully abort the download
    state.currentId = null;
  }

  function stop() {
    stopPlaybackOnly();
    update(state.stations);
  }

  // Synthetic clicks (and accidental double taps) can deliver two events;
  // a play/stop flip-flop within 200 ms is never intentional.
  function debounceToggle(fn) {
    var last = 0;
    return function () {
      var now = Date.now();
      if (now - last < 200) return;
      last = now;
      fn();
    };
  }

  playerToggle.addEventListener("click", debounceToggle(function () {
    var playing = audio && state.currentId && !audio.paused && !audio.ended;
    if (playing) { stop(); }
    else if (state.lastId) { play(state.lastId); }
  }));

  window.addEventListener("resize", function () {
    if (!viz.raf) vizIdle();  // playing loop re-fits every frame anyway
  });

  // ---------------------------------------------------------------- polling

  function poll() {
    fetchJSON(authUrl(BASE + "api/stations"))
      .then(function (data) {
        hideBanner();
        applyPlayerConfig(data.player);
        render(data.stations || []);
      })
      .catch(function (err) {
        conn.textContent = "offline";
        showBanner(err.message || "Cannot reach the server.");
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });

  refreshThemeButton();
  renderHistory();
  vizIdle();
  poll();
  setInterval(poll, POLL_MS);
})();
