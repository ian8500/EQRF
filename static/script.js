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
    toggle.textContent = mode === "day" ? "Night Mode" : "Day Mode";
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
    const PAGE_BUFFER = 2;
    const OBSERVER_MARGIN = "900px 0px";
    const state = {
      scale: 1,
      rotation: 0,
      defaultRotation: 0,
      mode: "custom",
      pdf: null,
      pageCount: 0,
      renderToken: 0,
      renderedPages: new Set(),
      renderingPages: new Set(),
      observer: null,
      pageText: new Map()
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
        stack: document.getElementById("pdf-page-stack"),
        frames: Array.from(shell.querySelectorAll(".pdf-page")),
        fitWidthButton: document.getElementById("pdf-fit-width"),
        fitHeightButton: document.getElementById("pdf-fit-height"),
        zoomIndicator: document.getElementById("pdf-zoom-indicator"),
        rotationIndicator: document.getElementById("pdf-rotation-indicator"),
        pageIndicator: document.getElementById("pdf-page-indicator")
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

    async function baseViewport() {
      if (!state.pdf) return null;
      const page = await state.pdf.getPage(1);
      return page.getViewport({ scale: 1, rotation: state.rotation });
    }

    async function calculateDefaultRotation(parts) {
      if (!state.pdf) return 0;
      try {
        const page = await state.pdf.getPage(1);
        const viewport = page.getViewport({ scale: 1, rotation: 0 });
        const naturalLandscape = viewport.width >= viewport.height;
        const documentOrientation = (parts?.shell?.dataset.orientation || "portrait").toLowerCase() === "landscape" ? "landscape" : "portrait";
        const desiredLandscape = documentOrientation === "landscape";
        return naturalLandscape === desiredLandscape ? 0 : 90;
      } catch (_) {
        return 0;
      }
    }

    async function scaleForWidth(parts) {
      if (!parts.scroll) return state.scale;
      const viewport = await baseViewport();
      if (!viewport) return state.scale;
      const available = Math.max(240, parts.scroll.clientWidth - MARGIN);
      return clamp(available / viewport.width);
    }

    async function scaleForHeight(parts) {
      if (!parts.scroll) return state.scale;
      const viewport = await baseViewport();
      if (!viewport) return state.scale;
      const available = Math.max(240, parts.scroll.clientHeight - MARGIN);
      return clamp(available / viewport.height);
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

    function escapeRegExp(value) {
      return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function buildTextSnippet(text, query, radius = 72) {
      const lower = text.toLowerCase();
      const at = lower.indexOf(query.toLowerCase());
      if (at < 0) return text.slice(0, 140);
      const left = Math.max(0, at - radius);
      const right = Math.min(text.length, at + query.length + radius);
      return `${left > 0 ? "... " : ""}${text.slice(left, right).replace(/\s+/g, " ").trim()}${right < text.length ? " ..." : ""}`;
    }

    async function renderTextLayer(page, frame, viewport) {
      if (frame.dataset.searchable !== "true") return;
      const textLayer = frame.querySelector(".pdf-text-layer");
      if (!textLayer) return;
      textLayer.innerHTML = "";
      try {
        const content = await page.getTextContent();
        state.pageText.set(Number(frame.dataset.pageNumber), content.items.map(item => item.str || "").join(" "));
        content.items.forEach(item => {
          const text = item.str || "";
          if (!text.trim()) return;
          const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
          const span = document.createElement("span");
          span.textContent = text;
          span.dataset.text = text;
          span.style.left = `${tx[4]}px`;
          span.style.top = `${tx[5]}px`;
          span.style.fontSize = `${Math.max(1, Math.abs(tx[0]))}px`;
          span.style.transform = `scaleX(${Math.max(0.4, Math.hypot(tx[0], tx[1]) / Math.max(1, Math.abs(tx[0])))})`;
          textLayer.appendChild(span);
        });
      } catch (_) {
        state.pageText.set(Number(frame.dataset.pageNumber), "");
      }
    }

    function pageBufferNumbers(centerPage) {
      const start = Math.max(1, centerPage - PAGE_BUFFER);
      const end = Math.min(state.pageCount || centerPage, centerPage + PAGE_BUFFER);
      const pages = [];
      for (let page = start; page <= end; page += 1) pages.push(page);
      return pages;
    }

    function clearPageCanvas(frame) {
      const pageNumber = Number(frame?.dataset.pageNumber || 0);
      const canvas = frame?.querySelector(".pdf-canvas");
      const textLayer = frame?.querySelector(".pdf-text-layer");
      const highlightLayer = frame?.querySelector(".pdf-highlight-layer");
      if (canvas) {
        canvas.width = 0;
        canvas.height = 0;
      }
      if (textLayer) textLayer.innerHTML = "";
      if (highlightLayer) highlightLayer.innerHTML = "";
      if (frame) delete frame.dataset.rendered;
      if (pageNumber) {
        state.renderedPages.delete(pageNumber);
        state.pageText.delete(pageNumber);
      }
    }

    function clearPagesOutsideBuffer(centerPage, keepExtra = []) {
      const keep = new Set([...pageBufferNumbers(centerPage), ...keepExtra.map(Number)]);
      document.querySelectorAll(".pdf-page[data-rendered='true']").forEach(frame => {
        const pageNumber = Number(frame.dataset.pageNumber || 0);
        if (pageNumber && !keep.has(pageNumber)) clearPageCanvas(frame);
      });
    }

    async function renderPage(pageNumber, token = state.renderToken) {
      if (!state.pdf) return;
      if (token !== state.renderToken) return;
      if (state.renderedPages.has(pageNumber)) return;
      const renderKey = `${token}:${pageNumber}`;
      if (state.renderingPages.has(renderKey)) return;
      const parts = viewer();
      const frame = document.querySelector(`.pdf-page[data-page-number="${pageNumber}"]`);
      const canvas = frame?.querySelector(".pdf-canvas");
      if (!parts || !frame || !canvas) return;
      state.renderingPages.add(renderKey);
      const page = await state.pdf.getPage(pageNumber);
      const viewport = page.getViewport({ scale: state.scale, rotation: state.rotation });
      const context = canvas.getContext("2d");
      const outputScale = Math.min(window.devicePixelRatio || 1, 2);

      frame.style.width = `${Math.ceil(viewport.width)}px`;
      frame.style.minHeight = `${Math.ceil(viewport.height)}px`;
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${Math.ceil(viewport.width)}px`;
      canvas.style.height = `${Math.ceil(viewport.height)}px`;
      const textLayer = frame.querySelector(".pdf-text-layer");
      const highlightLayer = frame.querySelector(".pdf-highlight-layer");
      [textLayer, highlightLayer].forEach(layer => {
        if (!layer) return;
        layer.style.width = `${Math.ceil(viewport.width)}px`;
        layer.style.height = `${Math.ceil(viewport.height)}px`;
      });

      const transform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
      try {
        await page.render({ canvasContext: context, viewport, transform }).promise;
        if (token !== state.renderToken) return;
        frame.dataset.rendered = "true";
        state.renderedPages.add(pageNumber);
        await renderTextLayer(page, frame, viewport);
        reapplyClientSearchHighlights();
      } finally {
        state.renderingPages.delete(renderKey);
      }
    }

    async function preparePageFrames() {
      const parts = viewer();
      if (!parts || !parts.frames.length || !state.pdf) return;
      const viewport = await baseViewport();
      if (!viewport) return;
      const width = Math.ceil(viewport.width * state.scale);
      const height = Math.ceil(viewport.height * state.scale);
      parts.frames.forEach(frame => {
        clearPageCanvas(frame);
        delete frame.dataset.rendered;
        frame.style.width = `${width}px`;
        frame.style.minHeight = `${height}px`;
        const surface = frame.querySelector(".pdf-page-surface");
        const canvas = frame.querySelector(".pdf-canvas");
        const textLayer = frame.querySelector(".pdf-text-layer");
        const highlightLayer = frame.querySelector(".pdf-highlight-layer");
        [surface, canvas, textLayer, highlightLayer].forEach(element => {
          if (!element) return;
          element.style.width = `${width}px`;
          element.style.height = `${height}px`;
        });
      });
    }

    function ensurePageFrames(parts) {
      if (!parts || !parts.stack || !state.pageCount) return;
      parts.stack.innerHTML = "";
      const searchable = parts.shell.dataset.canSearch === "true";
      for (let pageNumber = 1; pageNumber <= state.pageCount; pageNumber += 1) {
        const frame = document.createElement("figure");
        frame.className = "pdf-page";
        frame.dataset.pageIndex = String(pageNumber - 1);
        frame.dataset.pageNumber = String(pageNumber);
        frame.dataset.searchable = searchable ? "true" : "false";
        frame.innerHTML = `
          <figcaption>Page ${pageNumber} of ${state.pageCount}</figcaption>
          <div class="pdf-page-surface">
            <canvas class="pdf-canvas"></canvas>
            ${searchable ? '<div class="pdf-text-layer" aria-hidden="true"></div><div class="pdf-highlight-layer" aria-hidden="true"></div>' : ''}
          </div>
        `;
        parts.stack.appendChild(frame);
      }
      parts.frames = Array.from(parts.shell.querySelectorAll(".pdf-page"));
    }

    function setupPageObserver(parts) {
      if (!parts || !parts.scroll || !parts.frames.length || !("IntersectionObserver" in window)) return;
      if (state.observer) state.observer.disconnect();
      state.observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          const pageNumber = Number(entry.target.dataset.pageNumber || 0);
          if (pageNumber) renderPageBuffer(pageNumber);
        });
      }, {
        root: parts.scroll,
        rootMargin: OBSERVER_MARGIN,
        threshold: 0.01
      });
      parts.frames.forEach(frame => state.observer.observe(frame));
    }

    async function applyViewerLayout(options = {}) {
      const parts = viewer();
      if (!parts || !parts.scroll || !parts.frames.length) return;
      const context = options.preserve === false ? { index: 0, ratio: 0 } : currentPageContext(parts);

      if (state.mode === "width") state.scale = await scaleForWidth(parts);
      if (state.mode === "height") state.scale = await scaleForHeight(parts);
      state.scale = clamp(state.scale);

      const token = ++state.renderToken;
      state.renderedPages = new Set();
      state.renderingPages = new Set();
      await preparePageFrames();
      setupPageObserver(parts);
      await renderVisiblePages(token);

      updateIndicators(parts);
      if (options.preserve !== false) restorePageContext(parts, context);
      reapplyClientSearchHighlights();
    }

    async function renderVisiblePages(token = state.renderToken) {
      const parts = viewer();
      if (!parts || !parts.scroll || !parts.frames.length || !state.pdf) return;
      const top = parts.scroll.scrollTop - parts.scroll.clientHeight;
      const bottom = parts.scroll.scrollTop + (parts.scroll.clientHeight * 1.75);
      const visible = parts.frames
        .filter(frame => frame.offsetTop + frame.offsetHeight >= top && frame.offsetTop <= bottom)
        .map(frame => Number(frame.dataset.pageNumber));
      const context = currentPageContext(parts);
      const centerPage = visible[0] || (context.index + 1) || 1;
      const pages = new Set([...(visible.length ? visible : [1]), ...pageBufferNumbers(centerPage)]);
      for (const pageNumber of pages) {
        await renderPage(pageNumber, token);
      }
      clearPagesOutsideBuffer(centerPage, visible);
      reapplyClientSearchHighlights();
    }

    async function renderPageBuffer(centerPage, token = state.renderToken) {
      for (const pageNumber of pageBufferNumbers(centerPage)) {
        await renderPage(pageNumber, token);
      }
      clearPagesOutsideBuffer(centerPage, searchState.results.map(result => result.page));
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
      state.rotation = state.defaultRotation;
      state.mode = "custom";
      state.scale = 1;
      applyViewerLayout({ preserve: false });
    }

    async function bindPdfViewer() {
      const parts = viewer();
      if (!parts || !parts.scroll) return;
      if (!window.pdfjsLib) {
        if (parts.stack) parts.stack.innerHTML = '<p class="page-copy">This PDF could not be loaded.</p>';
        return;
      }
      document.getElementById("pdf-zoom-in")?.addEventListener("click", zoomIn);
      document.getElementById("pdf-zoom-out")?.addEventListener("click", zoomOut);
      document.getElementById("pdf-fit-width")?.addEventListener("click", fitWidth);
      document.getElementById("pdf-fit-height")?.addEventListener("click", fitHeight);
      document.getElementById("pdf-rotate")?.addEventListener("click", rotate);
      document.getElementById("pdf-reset")?.addEventListener("click", resetViewer);
      let scrollRenderTimer = null;
      parts.scroll.addEventListener("scroll", () => {
        updateCurrentPage();
        if (scrollRenderTimer) window.clearTimeout(scrollRenderTimer);
        scrollRenderTimer = window.setTimeout(() => renderVisiblePages(), 80);
      }, { passive: true });
      let resizeTimer = null;
      const scheduleViewerResize = () => {
        if (typeof window.eqrfSetLayoutHeights === "function") window.eqrfSetLayoutHeights();
        if (resizeTimer) window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(() => {
          if (state.mode === "width" || state.mode === "height") {
            applyViewerLayout();
          } else {
            updateIndicators(viewer());
            renderVisiblePages();
          }
        }, 120);
      };
      window.addEventListener("resize", scheduleViewerResize);
      window.addEventListener("orientationchange", scheduleViewerResize);
      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", scheduleViewerResize);
      }
      try {
        state.pdf = await pdfjsLib.getDocument(parts.shell.dataset.pdfUrl).promise;
        state.pageCount = state.pdf.numPages || Number(parts.shell.dataset.pageCount) || 0;
        state.defaultRotation = await calculateDefaultRotation(parts);
        state.rotation = state.defaultRotation;
        ensurePageFrames(parts);
        setupPageObserver(viewer());
        if (typeof window.eqrfSetLayoutHeights === "function") window.eqrfSetLayoutHeights();
        updateIndicators(viewer());
        await applyViewerLayout({ preserve: false });
      } catch (_) {
        if (parts.stack) parts.stack.innerHTML = '<p class="page-copy">This PDF could not be loaded.</p>';
      }
    }

    const searchState = {
      results: [],
      currentIndex: -1,
      query: ""
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
      document.querySelectorAll(".pdf-page.search-hit, .pdf-page.search-current").forEach(frame => {
        frame.classList.remove("search-hit", "search-current");
      });
      document.querySelectorAll(".pdf-search-result.active").forEach(result => {
        result.classList.remove("active");
      });
      document.querySelectorAll(".pdf-text-layer span.search-match").forEach(span => {
        span.classList.remove("search-match");
      });
    }

    function reapplyClientSearchHighlights() {
      if (!searchState.query) return;
      const pattern = new RegExp(escapeRegExp(searchState.query), "i");
      document.querySelectorAll(".pdf-text-layer span").forEach(span => {
        span.classList.toggle("search-match", pattern.test(span.dataset.text || span.textContent || ""));
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
        const frame = document.querySelector(`.pdf-page[data-page-number="${result.page}"]`);
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

    async function goToSearchResult(index) {
      if (!searchState.results.length) return;
      searchState.currentIndex = (index + searchState.results.length) % searchState.results.length;
      clearSearchHighlights();
      searchState.results.forEach(result => {
        const hit = document.querySelector(`.pdf-page[data-page-number="${result.page}"]`);
        if (hit) hit.classList.add("search-hit");
      });
      const result = searchState.results[searchState.currentIndex];
      const frame = document.querySelector(`.pdf-page[data-page-number="${result.page}"]`);
      if (frame) {
        frame.classList.add("search-current");
        frame.scrollIntoView({ behavior: "smooth", block: "start" });
        await renderPageBuffer(Number(result.page || frame.dataset.pageNumber || 1));
        frame.classList.add("search-current");
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
      searchState.query = query;
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
          renderSearchResults("No searchable text found in this PDF.");
          return;
        }
        renderSearchResults();
        reapplyClientSearchHighlights();
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
        searchState.query = "";
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
  
