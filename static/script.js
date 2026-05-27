// === CHECKLIST STATE MANAGEMENT ===

// Save checklist state to localStorage
function saveChecklistState() {
    const checklist = document.querySelectorAll('#checklist input[type="checkbox"]');
    if (!checklist.length) return;
    const state = Array.from(checklist).map(checkbox => checkbox.checked);
    localStorage.setItem(window.location.pathname, JSON.stringify(state));
  }
  
  // Load checklist state from localStorage
  function loadChecklistState() {
    const checklist = document.querySelectorAll('#checklist input[type="checkbox"]');
    if (!checklist.length) return;
    const saved = localStorage.getItem(window.location.pathname);
    if (!saved) return;
    try {
      const savedState = JSON.parse(saved);
      if (Array.isArray(savedState) && savedState.length === checklist.length) {
        checklist.forEach((checkbox, index) => {
          checkbox.checked = !!savedState[index];
          highlightTicked(checkbox);
        });
      }
    } catch (_) { /* ignore bad JSON */ }
  }
  
  // Apply green fill when checked
  function highlightTicked(checkbox) {
    if (!checkbox) return;
    const label = checkbox.parentElement;
    if (!label) return;
    if (checkbox.checked) {
      label.classList.add("checked");
    } else {
      label.classList.remove("checked");
    }
    updateChecklistProgress();
    saveChecklistState();
  }

  function updateChecklistProgress() {
    const checklist = document.querySelectorAll('#checklist input[type="checkbox"]');
    const progress = document.getElementById("checklist-progress");
    if (!progress || !checklist.length) return;
    const complete = Array.from(checklist).filter(cb => cb.checked).length;
    progress.textContent = `${complete} / ${checklist.length} complete`;
  }
  
  // Reset checklist and clear stored state
  function resetChecklist() {
    const checklist = document.querySelectorAll('#checklist input[type="checkbox"]');
    checklist.forEach(cb => {
      cb.checked = false;
      const label = cb.parentElement;
      if (label) label.classList.remove("checked");
    });
    localStorage.removeItem(window.location.pathname);
    updateChecklistProgress();
  }

  function clearCompletedChecklist() {
    const checked = document.querySelectorAll('#checklist input[type="checkbox"]:checked');
    checked.forEach(cb => {
      cb.checked = false;
      const label = cb.parentElement;
      if (label) label.classList.remove("checked");
    });
    saveChecklistState();
    updateChecklistProgress();
  }
  
  
  // === DAY / NIGHT MODE TOGGLE ===
  function getSavedTheme() {
    const current = localStorage.getItem("eqrf-theme") || localStorage.getItem("theme");
    if (current === "day" || current === "light") return "day";
    return "night";
  }

  function setTheme(mode) {
    const resolved = mode === "day" ? "day" : "night";
    const root = document.documentElement;
    root.classList.remove("theme-day", "theme-night", "night-mode");
    root.classList.add(resolved === "day" ? "theme-day" : "theme-night");
    if (resolved === "night") root.classList.add("night-mode");
    localStorage.setItem("eqrf-theme", resolved);
    localStorage.setItem("theme", resolved);
    localStorage.setItem("nightMode", resolved === "night" ? "on" : "off");
    updateThemeToggleText(resolved);
  }

  function updateThemeToggleText(mode) {
    const toggle = document.getElementById("theme-toggle");
    if (!toggle) return;
    toggle.textContent = mode === "day" ? "Day Mode" : "Night Mode";
    toggle.setAttribute("aria-pressed", mode === "night" ? "true" : "false");
  }

  function loadTheme() {
    setTheme(getSavedTheme());
  }

  function toggleTheme() {
    const current = document.documentElement.classList.contains("theme-day") ? "day" : "night";
    setTheme(current === "day" ? "night" : "day");
  }
  
  
  // === SERVER-SENT EVENTS (SSE) FOR REFRESH ===
  if (typeof EventSource !== "undefined") {
    const evtSource = new EventSource("/stream");
    evtSource.addEventListener("refresh", async () => {
      const current = window.location.pathname + window.location.search;
      try {
        const response = await fetch("/resolve-refresh-target?current=" + encodeURIComponent(current), {
          credentials: "same-origin"
        });
        if (response.ok) {
          const data = await response.json();
          if (data && data.target && typeof data.target === "string") {
            if (data.target === current || data.target === window.location.pathname) {
              window.location.reload();
            } else {
              window.location.href = data.target;
            }
            return;
          }
        }
      } catch (_) { /* fall back to normal reload */ }
      window.location.reload();
    });
  }


  // === PDF VIEWER CONTROLS ===
  (function pdfViewerControls() {
    const MIN_SCALE = 0.25;
    const MAX_SCALE = 4;
    const STEP = 0.1;
    const MARGIN = 24;
    const state = {
      scale: 1,
      rotation: 0,
      mode: "width"
    };

    function clamp(value) {
      return Math.max(MIN_SCALE, Math.min(MAX_SCALE, value));
    }

    function viewer() {
      const shell = document.querySelector(".pdf-viewer-shell");
      if (!shell) return null;
      return {
        shell,
        scroll: document.getElementById("pdf-scroll"),
        frames: Array.from(shell.querySelectorAll(".pdf-page-frame")),
        images: Array.from(shell.querySelectorAll(".pdf-page-image")),
        fitWidthButton: document.getElementById("pdf-fit-width"),
        fitHeightButton: document.getElementById("pdf-fit-height"),
        zoomIndicator: document.getElementById("pdf-zoom-indicator"),
        rotationIndicator: document.getElementById("pdf-rotation-indicator"),
        pageIndicator: document.getElementById("pdf-page-indicator")
      };
    }

    function dimensionsFor(img, scale = state.scale, rotation = state.rotation) {
      const naturalWidth = img.naturalWidth || Number(img.dataset.naturalWidth) || 1;
      const naturalHeight = img.naturalHeight || Number(img.dataset.naturalHeight) || 1;
      const imageWidth = naturalWidth * scale;
      const imageHeight = naturalHeight * scale;
      const rotated = rotation % 180 !== 0;
      return {
        imageWidth,
        imageHeight,
        layoutWidth: rotated ? imageHeight : imageWidth,
        layoutHeight: rotated ? imageWidth : imageHeight,
        naturalLayoutWidth: rotated ? naturalHeight : naturalWidth,
        naturalLayoutHeight: rotated ? naturalWidth : naturalHeight
      };
    }

    function currentPageContext(parts) {
      if (!parts || !parts.scroll || !parts.frames.length) return { index: 0, ratio: 0 };
      const scrollTop = parts.scroll.scrollTop;
      let best = { index: 0, distance: Infinity, ratio: 0 };
      parts.frames.forEach((frame, index) => {
        const distance = Math.abs(frame.offsetTop - scrollTop);
        if (distance < best.distance) {
          const ratio = frame.offsetHeight > 0 ? (scrollTop - frame.offsetTop) / frame.offsetHeight : 0;
          best = { index, distance, ratio: Math.max(0, Math.min(0.95, ratio)) };
        }
      });
      return best;
    }

    function restorePageContext(parts, context) {
      if (!parts || !parts.scroll || !parts.frames.length) return;
      const frame = parts.frames[Math.min(context.index || 0, parts.frames.length - 1)];
      if (!frame) return;
      parts.scroll.scrollTop = Math.max(0, frame.offsetTop + (frame.offsetHeight * (context.ratio || 0)));
    }

    function firstReadyImage(parts) {
      return parts.images.find(img => img.naturalWidth && img.naturalHeight) || parts.images[0];
    }

    function scaleForWidth(parts) {
      const img = firstReadyImage(parts);
      if (!img || !parts.scroll) return state.scale;
      const dims = dimensionsFor(img, 1, state.rotation);
      const available = Math.max(240, parts.scroll.clientWidth - MARGIN);
      return clamp(available / dims.naturalLayoutWidth);
    }

    function scaleForHeight(parts) {
      const img = firstReadyImage(parts);
      if (!img || !parts.scroll) return state.scale;
      const dims = dimensionsFor(img, 1, state.rotation);
      const available = Math.max(240, parts.scroll.clientHeight - MARGIN);
      return clamp(available / dims.naturalLayoutHeight);
    }

    function updateIndicators(parts) {
      if (!parts) return;
      if (parts.zoomIndicator) {
        const label = state.mode === "width" ? "Fit Width" : state.mode === "height" ? "Fit Height" : `${Math.round(state.scale * 100)}%`;
        parts.zoomIndicator.textContent = label;
      }
      if (parts.rotationIndicator) parts.rotationIndicator.textContent = `${state.rotation}°`;
      if (parts.fitWidthButton) parts.fitWidthButton.classList.toggle("viewer-control-active", state.mode === "width");
      if (parts.fitHeightButton) parts.fitHeightButton.classList.toggle("viewer-control-active", state.mode === "height");
      updateCurrentPage(parts);
    }

    function applyViewerLayout(options = {}) {
      const parts = viewer();
      if (!parts || !parts.scroll || !parts.frames.length) return;
      const context = options.preserve === false ? { index: 0, ratio: 0 } : currentPageContext(parts);

      if (state.mode === "width") state.scale = scaleForWidth(parts);
      if (state.mode === "height") state.scale = scaleForHeight(parts);
      state.scale = clamp(state.scale);

      parts.frames.forEach((frame, index) => {
        const img = parts.images[index];
        if (!img) return;
        if (!img.naturalWidth || !img.naturalHeight) return;
        const dims = dimensionsFor(img);
        frame.style.width = `${Math.round(dims.layoutWidth)}px`;
        frame.style.height = `${Math.round(dims.layoutHeight)}px`;
        img.style.width = `${Math.round(dims.imageWidth)}px`;
        img.style.height = `${Math.round(dims.imageHeight)}px`;
        img.style.transform = `translate(-50%, -50%) rotate(${state.rotation}deg)`;
      });

      updateIndicators(parts);
      if (options.preserve !== false) restorePageContext(parts, context);
    }

    function updateCurrentPage(parts = viewer()) {
      if (!parts || !parts.scroll || !parts.frames.length || !parts.pageIndicator) return;
      const midpoint = parts.scroll.scrollTop + (parts.scroll.clientHeight * 0.35);
      let current = 0;
      parts.frames.forEach((frame, index) => {
        if (frame.offsetTop <= midpoint) current = index;
      });
      parts.pageIndicator.textContent = `Page ${current + 1} / ${parts.frames.length}`;
    }

    function fitWidth() {
      state.mode = "width";
      applyViewerLayout();
    }

    function fitHeight() {
      state.mode = "height";
      applyViewerLayout();
    }

    function zoomIn() {
      state.mode = "custom";
      state.scale = clamp(state.scale + STEP);
      applyViewerLayout();
    }

    function zoomOut() {
      state.mode = "custom";
      state.scale = clamp(state.scale - STEP);
      applyViewerLayout();
    }

    function rotate() {
      state.rotation = (state.rotation + 90) % 360;
      applyViewerLayout();
    }

    function resetViewer() {
      state.rotation = 0;
      state.mode = "width";
      applyViewerLayout({ preserve: false });
    }

    function bindPdfViewer() {
      const parts = viewer();
      if (!parts || !parts.scroll) return;
      document.getElementById("pdf-zoom-in")?.addEventListener("click", zoomIn);
      document.getElementById("pdf-zoom-out")?.addEventListener("click", zoomOut);
      document.getElementById("pdf-fit-width")?.addEventListener("click", fitWidth);
      document.getElementById("pdf-fit-height")?.addEventListener("click", fitHeight);
      document.getElementById("pdf-rotate")?.addEventListener("click", rotate);
      document.getElementById("pdf-reset")?.addEventListener("click", resetViewer);
      parts.scroll.addEventListener("scroll", () => updateCurrentPage(), { passive: true });
      parts.images.forEach(img => {
        if (img.complete && img.naturalWidth) {
          applyViewerLayout({ preserve: false });
        } else {
          img.addEventListener("load", () => applyViewerLayout({ preserve: false }), { once: true });
        }
      });
      window.addEventListener("resize", () => {
        if (state.mode === "width" || state.mode === "height") applyViewerLayout();
      });
      applyViewerLayout({ preserve: false });
    }

    const searchState = {
      results: [],
      currentIndex: -1
    };

    function searchElements() {
      return {
        shell: document.querySelector(".pdf-viewer-shell"),
        form: document.getElementById("pdf-search-form"),
        input: document.getElementById("pdf-search-input"),
        prev: document.getElementById("pdf-search-prev"),
        next: document.getElementById("pdf-search-next"),
        clear: document.getElementById("pdf-search-clear"),
        count: document.getElementById("pdf-search-count"),
        results: document.getElementById("pdf-search-results")
      };
    }

    function setSearchCount() {
      const els = searchElements();
      const total = searchState.results.length;
      const current = total && searchState.currentIndex >= 0 ? searchState.currentIndex + 1 : 0;
      if (els.count) els.count.textContent = `${current} / ${total}`;
    }

    function clearSearchHighlights() {
      document.querySelectorAll(".pdf-page-frame.search-hit, .pdf-page-frame.search-current").forEach(frame => {
        frame.classList.remove("search-hit", "search-current");
      });
      document.querySelectorAll(".pdf-search-result.active").forEach(result => {
        result.classList.remove("active");
      });
    }

    function renderSearchResults(message = "") {
      const els = searchElements();
      if (!els.results) return;
      els.results.innerHTML = "";
      clearSearchHighlights();
      if (message) {
        els.results.hidden = false;
        const note = document.createElement("p");
        note.className = "page-copy";
        note.textContent = message;
        els.results.appendChild(note);
        setSearchCount();
        return;
      }
      if (!searchState.results.length) {
        els.results.hidden = true;
        setSearchCount();
        return;
      }
      els.results.hidden = false;
      searchState.results.forEach((result, index) => {
        const frame = document.querySelector(`.pdf-page-frame[data-page-number="${result.page}"]`);
        if (frame) frame.classList.add("search-hit");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "pdf-search-result";
        button.dataset.resultIndex = String(index);
        button.innerHTML = `<strong>Page ${result.page}</strong><span>${result.snippet || ""}</span>`;
        button.addEventListener("click", () => goToSearchResult(index));
        els.results.appendChild(button);
      });
      setSearchCount();
    }

    function goToSearchResult(index) {
      if (!searchState.results.length) return;
      searchState.currentIndex = (index + searchState.results.length) % searchState.results.length;
      clearSearchHighlights();
      searchState.results.forEach(result => {
        const hit = document.querySelector(`.pdf-page-frame[data-page-number="${result.page}"]`);
        if (hit) hit.classList.add("search-hit");
      });
      const result = searchState.results[searchState.currentIndex];
      const frame = document.querySelector(`.pdf-page-frame[data-page-number="${result.page}"]`);
      if (frame) {
        frame.classList.add("search-current");
        frame.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      const resultButton = document.querySelector(`.pdf-search-result[data-result-index="${searchState.currentIndex}"]`);
      if (resultButton) resultButton.classList.add("active");
      setSearchCount();
      updateCurrentPage();
    }

    async function runPdfSearch(event) {
      if (event) event.preventDefault();
      const els = searchElements();
      if (!els.shell || !els.input) return;
      const query = els.input.value.trim();
      searchState.results = [];
      searchState.currentIndex = -1;
      if (!query) {
        renderSearchResults();
        return;
      }
      renderSearchResults("Searching...");
      const params = new URLSearchParams({
        category: els.shell.dataset.category || "",
        filename: els.shell.dataset.filename || "",
        q: query
      });
      try {
        const response = await fetch(`/viewer-search?${params.toString()}`, { credentials: "same-origin" });
        const data = await response.json();
        if (!response.ok || data.error) {
          renderSearchResults(data.error || "This PDF could not be text searched.");
          return;
        }
        searchState.results = Array.isArray(data.results) ? data.results : [];
        searchState.currentIndex = searchState.results.length ? 0 : -1;
        if (!searchState.results.length) {
          renderSearchResults("No matches found.");
          return;
        }
        renderSearchResults();
        goToSearchResult(0);
      } catch (_) {
        renderSearchResults("This PDF could not be text searched.");
      }
    }

    function bindPdfSearch() {
      const els = searchElements();
      if (!els.form) return;
      els.form.addEventListener("submit", runPdfSearch);
      els.prev?.addEventListener("click", () => goToSearchResult(searchState.currentIndex - 1));
      els.next?.addEventListener("click", () => goToSearchResult(searchState.currentIndex + 1));
      els.clear?.addEventListener("click", () => {
        searchState.results = [];
        searchState.currentIndex = -1;
        if (els.input) els.input.value = "";
        renderSearchResults();
      });
      setSearchCount();
    }

    window.zoomIn = zoomIn;
    window.zoomOut = zoomOut;
    window.fitWidth = fitWidth;
    window.fitHeight = fitHeight;
    window.rotate = rotate;
    window.fitToPage = resetViewer;
    document.addEventListener("DOMContentLoaded", bindPdfViewer);
    document.addEventListener("DOMContentLoaded", bindPdfSearch);
  })();
  
  
  // === HEADER OFFSET WATCHER (for sticky header/toolbar/layout) ===
  (function headerOffsetWatcher() {
    function updateOffsets() {
      const header = document.querySelector(".site-header");
      const h = header ? header.offsetHeight : 0;
      document.documentElement.style.setProperty("--header-h", h + "px");
    }
    window.addEventListener("load", updateOffsets, { passive: true });
    window.addEventListener("resize", updateOffsets);
    document.addEventListener("DOMContentLoaded", updateOffsets);
  })();
  
  
  // === INITIALISE EVERYTHING ON PAGE LOAD ===
  document.addEventListener("DOMContentLoaded", () => {
    // Checklist state
    loadChecklistState();
  
    const checklist = document.querySelectorAll('#checklist input[type="checkbox"]');
    checklist.forEach(cb => {
      // Initialise green fill for any pre-checked items
      highlightTicked(cb);
      cb.addEventListener("change", () => highlightTicked(cb));
    });
    updateChecklistProgress();
  
    // Theme
    loadTheme();
    const modeToggle = document.getElementById("theme-toggle");
    if (modeToggle) {
      modeToggle.addEventListener("click", toggleTheme);
    }
  });
  
