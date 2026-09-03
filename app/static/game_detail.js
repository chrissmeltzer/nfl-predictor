(function () {
  "use strict";

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

  document.addEventListener("DOMContentLoaded", function () {
    animateProbabilityBars();
  });
})();
