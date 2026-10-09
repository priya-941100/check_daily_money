window.PALETTE = [
  "#6366f1",
  "#06b6d4",
  "#10b981",
  "#f59e0b",
  "#f43f5e",
  "#8b5cf6",
  "#14b8a6",
  "#fb923c",
  "#64748b",
];

function chartTheme() {
  if (typeof Chart === "undefined") return;
  var s = getComputedStyle(document.documentElement);
  Chart.defaults.color = s.getPropertyValue("--mute").trim();
  Chart.defaults.borderColor = s.getPropertyValue("--line").trim();
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
}
function refreshCharts() {
  if (typeof Chart === "undefined") return;
  chartTheme();
  Object.values(Chart.instances).forEach(function (ch) {
    var o = ch.options,
      c = Chart.defaults.color,
      g = Chart.defaults.borderColor;
    Object.values(o.scales || {}).forEach(function (sc) {
      sc.ticks.color = c;
      sc.grid.color = g;
    });
    if (o.plugins && o.plugins.legend && o.plugins.legend.labels)
      o.plugins.legend.labels.color = c;
    ch.update();
  });
}
chartTheme();

document.addEventListener("DOMContentLoaded", function () {
  var root = document.documentElement;

  // dark / light theme
  function icons() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.textContent = root.dataset.theme === "dark" ? "☀️" : "🌙";
    });
  }
  icons();
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("theme", root.dataset.theme);
      } catch (e) {}
      icons();
      try {
        refreshCharts();
      } catch (e) {}
    });
  });

  // mobile menu
  var m = document.getElementById("menuBtn"),
    o = document.getElementById("overlay");
  if (m)
    m.onclick = function () {
      document.body.classList.toggle("nav-open");
    };
  if (o)
    o.onclick = function () {
      document.body.classList.remove("nav-open");
    };

  // toasts fade out
  document.querySelectorAll(".toast").forEach(function (t, i) {
    setTimeout(
      function () {
        t.classList.add("hide");
        setTimeout(function () {
          t.remove();
        }, 400);
      },
      4200 + i * 400,
    );
  });

  // greeting by the visitor's local time
  var g = document.getElementById("greet");
  if (g) {
    var h = new Date().getHours();
    g.textContent =
      h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  }

  // animated numbers
  document.querySelectorAll("[data-count]").forEach(function (el) {
    var end = parseFloat(el.dataset.count) || 0,
      t0 = performance.now();
    function fmt(v) {
      return (
        (v < 0 ? "-" : "") +
        "₹" +
        Math.abs(v).toLocaleString("en-IN", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })
      );
    }
    (function step(t) {
      var p = Math.min((t - t0) / 900, 1),
        e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(end * e);
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  });

  // ask before deleting anything
  document.querySelectorAll("form button.sm").forEach(function (b) {
    b.closest("form").addEventListener("submit", function (e) {
      if (!confirm("Are you sure?")) e.preventDefault();
    });
  });

  // show / hide PIN
  document.querySelectorAll("[data-eye]").forEach(function (b) {
    b.addEventListener("click", function () {
      var i = b.previousElementSibling;
      i.type = i.type === "password" ? "text" : "password";
    });
  });
});
