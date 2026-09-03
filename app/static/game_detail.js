(function () {
  "use strict";

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (err) {
      return null;
    }
  }

  function animateProbabilityBars() {
    var bars = document.querySelectorAll(".probability-track i[data-target]");
    if (!bars.length) return;
    // A short timeout (rather than requestAnimationFrame, which never fires on a
    // backgrounded/hidden tab) guarantees the width change happens on a later tick
    // than the initial 0-width paint, so the CSS transition actually animates.
    setTimeout(function () {
      bars.forEach(function (bar) {
        bar.style.width = bar.getAttribute("data-target") + "%";
      });
    }, 50);
  }

  function renderTimelineChart() {
    var canvas = document.getElementById("h2h-chart");
    var data = readJson("h2h-data");
    if (!canvas || !data || !data.labels.length || typeof Chart === "undefined") return;

    var styles = getComputedStyle(document.documentElement);
    var cyan = styles.getPropertyValue("--cyan").trim();
    var violet = styles.getPropertyValue("--violet").trim();
    var muted = styles.getPropertyValue("--muted").trim();
    var line = styles.getPropertyValue("--line").trim();

    new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: data.home_abbr,
            data: data.home,
            borderColor: violet,
            backgroundColor: violet,
            tension: 0.35,
            pointRadius: 4,
            pointHoverRadius: 6,
            borderWidth: 2,
          },
          {
            label: data.away_abbr,
            data: data.away,
            borderColor: cyan,
            backgroundColor: cyan,
            tension: 0.35,
            pointRadius: 4,
            pointHoverRadius: 6,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: muted }, grid: { color: line } },
          y: { ticks: { color: muted }, grid: { color: line }, beginAtZero: true },
        },
      },
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    animateProbabilityBars();
    renderTimelineChart();
  });
})();
