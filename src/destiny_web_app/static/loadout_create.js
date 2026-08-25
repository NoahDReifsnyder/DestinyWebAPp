(() => {
  const form = document.querySelector("[data-import-wizard]");
  if (!form) return;
  const characterPicker = form.querySelector("[data-character-picker]");
  const groups = [...form.querySelectorAll("[data-import-character]")];
  const savedLoadouts = [...form.querySelectorAll("[data-edit-loadout]")];
  const savedPreview = form.querySelector("[data-saved-preview]");
  const selectedLoadoutPreview = form.querySelector("[data-selected-loadout-preview]");
  const selectedLoadoutEditor = form.querySelector("[data-selected-loadout-editor]");
  const selectedLoadoutId = form.querySelector("[data-selected-loadout-id]");
  const selectedLoadoutName = form.querySelector("[data-selected-loadout-name]");
  const destinyApiName = form.querySelector("[data-destiny-api-name]");
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[char]);
  const duplicateToggle = form.querySelector("[data-duplicate-toggle]");
  const duplicateResults = form.querySelector("[data-duplicate-results]");
  if (!characterPicker) return;
  const applyDefaultCharacter = (value) => {
    const option = [...characterPicker.options].find((candidate) =>
      candidate.value === value || candidate.textContent.trim().toLowerCase().startsWith(["titan", "hunter", "warlock"][Number(value)] || "titan")
    );
    if (option) characterPicker.value = option.value;
  };
  applyDefaultCharacter(window.destinyDefaultCharacter || "0");
  document.addEventListener("destiny-default-character-changed", (event) => {
    if (event.detail) {
      applyDefaultCharacter(event.detail);
      showCharacter();
    }
  });
  duplicateToggle?.addEventListener("click", () => {
    duplicateResults?.toggleAttribute("hidden");
    duplicateToggle.textContent = duplicateResults?.hidden ? "Check for duplicate loadouts" : "Hide duplicate loadouts";
  });

  const showCharacter = () => {
    groups.forEach((group) => {
      group.hidden = group.dataset.importCharacter !== characterPicker.value;
    });
    savedLoadouts.forEach((loadout) => {
      loadout.hidden = loadout.dataset.characterId !== characterPicker.value;
      loadout.classList.remove("selected");
    });
    if (selectedLoadoutPreview) selectedLoadoutPreview.innerHTML = '<p class="loadout-preview-empty">Select a saved loadout to preview its equipment.</p>';
    if (selectedLoadoutEditor) selectedLoadoutEditor.hidden = true;
    if (savedPreview) savedPreview.hidden = !savedLoadouts.some((loadout) => loadout.dataset.characterId === characterPicker.value);
  };
  characterPicker.addEventListener("change", showCharacter);
  form.addEventListener("click", (event) => {
    const loadout = event.target.closest("[data-edit-loadout]");
    if (!loadout || loadout.hidden) return;
    savedLoadouts.forEach((row) => row.classList.remove("selected"));
    loadout.classList.add("selected");
    selectedLoadoutId.value = loadout.dataset.loadoutId;
    selectedLoadoutName.value = loadout.dataset.loadoutName;
    destinyApiName.textContent = loadout.dataset.destinyApiName;
    selectedLoadoutEditor.hidden = false;
    let items = [];
    try { items = JSON.parse(loadout.dataset.previewJson || "[]"); } catch { items = []; }
    const image = (item, compact = false) => item.icon_path
      ? `<figure class="preview-item${compact ? " compact" : ""}"><img src="https://www.bungie.net${escapeHtml(item.icon_path)}" alt=""><figcaption>${escapeHtml(item.name)}</figcaption></figure>`
      : `<figure class="preview-item${compact ? " compact" : ""}"><span class="icon-fallback">?</span><figcaption>${escapeHtml(item.name)}</figcaption></figure>`;
    const modList = (mods) => mods.length ? `<div class="preview-mods">${mods.map((mod) => image(mod, true)).join("")}</div>` : "";
    const row = (item) => `<div class="detailed-preview-row"><div class="preview-main">${image(item)}</div>${modList(item.mods || [])}</div>`;
    const group = (title, groupItems) => groupItems.length ? `<section><h4>${title}</h4>${groupItems.map((item) => image(item, true)).join("")}</section>` : "";
    const subclass = items.subclass || { super: [], aspects: [], abilities: [], fragments: [] };
    selectedLoadoutPreview.innerHTML = `<h3>${escapeHtml(loadout.dataset.loadoutName)}</h3><div class="detailed-preview-weapons"><h4>Weapons</h4>${items.weapons.map(row).join("") || '<p class="loadout-preview-empty">No weapons saved.</p>'}</div><div class="detailed-preview-lower"><section><h4>Armor</h4>${items.armor.map(row).join("") || '<p class="loadout-preview-empty">No armor saved.</p>'}</section><section class="detailed-preview-subclass"><h4>Subclass</h4>${group("Super / Aspects", [...subclass.super, ...subclass.aspects])}${group("Abilities", subclass.abilities)}${group("Fragments", subclass.fragments)}</section></div>`;
  });
  form.addEventListener("click", (event) => {
  });
  showCharacter();
})();
