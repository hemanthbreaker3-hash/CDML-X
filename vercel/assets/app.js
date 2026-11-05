(() => {
  const host = document.getElementById("host");
  const copy = document.getElementById("copy-url");
  const toast = document.getElementById("toast");
  const origin = window.location.origin;
  host.textContent = origin.replace(/^https?:\/\//, "");
  document.getElementById("year").textContent = `© ${new Date().getFullYear()}`;
  fetch("/api/config").then(r => r.ok ? r.json() : null).then(cfg => {
    if (!cfg) return;
    if (cfg.site_name) {
      document.title = `${cfg.site_name} — Media Hub`;
      document.querySelectorAll(".brand > span:last-child").forEach(el => {
        el.innerHTML = `${String(cfg.site_name).replace(/BOTS$/, '<span class="brand-light">BOTS</span>')}`;
      });
    }
    if (cfg.site_tagline) document.querySelector(".hero-copy").textContent = cfg.site_tagline;
  }).catch(() => {});
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(origin);
      toast.textContent = "Website URL copied ✨";
    } catch (_) {
      toast.textContent = origin;
    }
    toast.classList.add("show");
    window.setTimeout(() => toast.classList.remove("show"), 2200);
  });
})();