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
    evtSource.addEventListener("refresh", () => {
      window.location.reload();
    });
  }
  
  
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
  
