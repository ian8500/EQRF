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
    saveChecklistState();
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
  }
  
  
  // === DARK MODE TOGGLE ===
  function loadTheme() {
    const savedMode = localStorage.getItem("theme");
    if (savedMode === "dark") {
      document.body.classList.add("dark");
    } else {
      document.body.classList.remove("dark");
    }
  }
  
  function toggleTheme() {
    document.body.classList.toggle("dark");
    const currentMode = document.body.classList.contains("dark") ? "dark" : "light";
    localStorage.setItem("theme", currentMode);
  }
  
  
  // === SERVER-SENT EVENTS (SSE) FOR REFRESH ===
  if (typeof EventSource !== "undefined") {
    const evtSource = new EventSource("/events");
    evtSource.onmessage = function (e) {
      if (e.data === "refresh") {
        window.location.reload();
      }
    };
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
  
    // Theme
    loadTheme();
    const modeToggle = document.getElementById("mode-toggle");
    if (modeToggle) {
      modeToggle.addEventListener("click", toggleTheme);
    }
  });
  