(() => {
  const form = document.querySelector("[data-import-wizard]");
  if (!form) return;

  const characterPicker = form.querySelector("[data-character-picker]");
  const groups = [...form.querySelectorAll("[data-import-character]")];
  const savedLoadoutList = form.querySelector("[data-saved-loadout-list]");
  const savedLoadouts = savedLoadoutList
    ? [...savedLoadoutList.querySelectorAll(":scope > [data-edit-loadout]")]
    : [];
  const savedPreview = form.querySelector("[data-saved-preview]");
  const selectedLoadoutPreview = document.getElementById(
    "selected-loadout-preview-content"
  );
  const selectedLoadoutEditor = form.querySelector(
    "[data-selected-loadout-editor]"
  );
  const selectedLoadoutId = form.querySelector("[data-selected-loadout-id]");
  const selectedLoadoutName = document.getElementById(
    "selected-loadout-name-input"
  );
  // Scoped to the editor: loadout tiles also carry data-destiny-api-name.
  const destinyApiName = selectedLoadoutEditor?.querySelector(
    "[data-destiny-api-name]"
  );
  const deleteForm = form.querySelector("[data-selected-loadout-delete]");
  const deleteLoadoutId = deleteForm?.querySelector("[data-delete-loadout-id]");
  const deleteRemoveFromSets = deleteForm?.querySelector(
    "[data-delete-remove-from-sets]"
  );
  const deleteSetWarning = deleteForm?.querySelector("[data-delete-set-warning]");
  const duplicateToggle = form.querySelector("[data-duplicate-toggle]");
  const duplicateResults = form.querySelector("[data-duplicate-results]");

  if (!characterPicker) return;

  const escapeHtml = (value) => String(value).replace(
    /[&<>'"]/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[character]
  );

  const applyDefaultCharacter = (value) => {
    const labels = ["titan", "hunter", "warlock"];
    const label = labels[Number(value)] || labels[0];
    const option = [...characterPicker.options].find(
      (candidate) => candidate.value === value
        || candidate.textContent.trim().toLowerCase().startsWith(label)
    );

    if (option) characterPicker.value = option.value;
  };

  const showCharacter = () => {
    groups.forEach((group) => {
      group.hidden = group.dataset.importCharacter !== characterPicker.value;
    });

    savedLoadouts.forEach((loadout) => {
      loadout.hidden = loadout.dataset.characterId !== characterPicker.value;
      loadout.classList.remove("selected");
    });

    if (selectedLoadoutPreview) {
      selectedLoadoutPreview.innerHTML =
        '<p class="loadout-preview-empty">Select a saved loadout to preview its equipment.</p>';
    }
    if (selectedLoadoutEditor) selectedLoadoutEditor.hidden = true;
    if (deleteForm) deleteForm.hidden = true;
    if (savedPreview) {
      savedPreview.hidden = !savedLoadouts.some(
        (loadout) => loadout.dataset.characterId === characterPicker.value
      );
    }
  };

  const renderImage = (item, compact = false) => item.icon_path
    ? `<figure class="preview-item${compact ? " compact" : ""}">
        <img src="https://www.bungie.net${escapeHtml(item.icon_path)}" alt="">
        <figcaption>${escapeHtml(item.name)}</figcaption>
      </figure>`
    : `<figure class="preview-item${compact ? " compact" : ""}">
        <span class="icon-fallback">?</span>
        <figcaption>${escapeHtml(item.name)}</figcaption>
      </figure>`;

  const renderModList = (mods) => mods.length
    ? `<div class="preview-mods">${mods.map((mod) => renderImage(mod, true)).join("")}</div>`
    : "";

  const renderEquipmentRow = (item) => `
    <div class="detailed-preview-row">
      <div class="preview-main">${renderImage(item)}</div>
      ${renderModList(item.mods || [])}
    </div>`;

  const renderSubclassGroup = (title, items) => items.length
    ? `<section><h4>${title}</h4>${items.map((item) => renderImage(item, true)).join("")}</section>`
    : "";

  const renderSelectedLoadout = (loadout) => {
    let preview = {};
    try {
      preview = JSON.parse(loadout.dataset.previewJson || "{}");
    } catch {
      preview = {};
    }

    const weapons = preview.weapons || [];
    const armor = preview.armor || [];
    const subclass = preview.subclass || {
      super: [],
      aspects: [],
      abilities: [],
      fragments: [],
    };

    return `
      <h3>${escapeHtml(loadout.dataset.loadoutName)}</h3>
      <div class="detailed-preview-weapons">
        <h4>Weapons</h4>
        ${weapons.map(renderEquipmentRow).join("")
          || '<p class="loadout-preview-empty">No weapons saved.</p>'}
      </div>
      <div class="detailed-preview-lower">
        <section>
          <h4>Armor</h4>
          ${armor.map(renderEquipmentRow).join("")
            || '<p class="loadout-preview-empty">No armor saved.</p>'}
        </section>
        <section class="detailed-preview-subclass">
          <h4>Subclass</h4>
          ${renderSubclassGroup("Super / Aspects", [
            ...subclass.super,
            ...subclass.aspects,
          ])}
          ${renderSubclassGroup("Abilities", subclass.abilities)}
          ${renderSubclassGroup("Fragments", subclass.fragments)}
        </section>
      </div>`;
  };

  applyDefaultCharacter(window.destinyDefaultCharacter || "0");

  document.addEventListener("destiny-default-character-changed", (event) => {
    if (!event.detail) return;
    applyDefaultCharacter(event.detail);
    showCharacter();
  });

  characterPicker.addEventListener("change", showCharacter);

  duplicateToggle?.addEventListener("click", () => {
    duplicateResults?.toggleAttribute("hidden");
    duplicateToggle.textContent = duplicateResults?.hidden
      ? "Check for duplicate loadouts"
      : "Hide duplicate loadouts";
  });

  const setNamesFor = (loadout) => {
    try {
      const names = JSON.parse(loadout.dataset.setNames || "[]");
      return Array.isArray(names) ? names : [];
    } catch {
      return [];
    }
  };

  const selectLoadout = (loadout) => {
    savedLoadouts.forEach((row) => row.classList.remove("selected"));
    loadout.classList.add("selected");
    selectedLoadoutId.value = loadout.dataset.loadoutId;
    selectedLoadoutName.value = loadout.dataset.loadoutName;
    destinyApiName.textContent = loadout.dataset.destinyApiName;
    selectedLoadoutEditor.hidden = false;
    selectedLoadoutPreview.innerHTML = renderSelectedLoadout(loadout);

    if (!deleteForm) return;
    const setNames = setNamesFor(loadout);
    deleteForm.hidden = false;
    deleteLoadoutId.value = loadout.dataset.loadoutId;
    deleteRemoveFromSets.value = "0";
    deleteSetWarning.hidden = !setNames.length;
    deleteSetWarning.textContent = setNames.length
      ? `Used by ${setNames.length} saved set(s): ${setNames.join(", ")}.`
      : "";
  };

  deleteForm?.addEventListener("submit", (event) => {
    const selected = savedLoadouts.find(
      (row) => row.dataset.loadoutId === deleteLoadoutId.value
    );
    const setNames = selected ? setNamesFor(selected) : [];
    const name = selected ? selected.dataset.loadoutName : "this loadout";
    const message = setNames.length
      ? `"${name}" is used by ${setNames.length} saved set(s): ${setNames.join(", ")}.\n\n`
        + "Delete it and remove it from those sets? This cannot be undone."
      : `Permanently delete "${name}"? This cannot be undone.`;

    if (!window.confirm(message)) {
      event.preventDefault();
      return;
    }
    deleteRemoveFromSets.value = setNames.length ? "1" : "0";
  });

  savedLoadouts.forEach((loadout) => {
    loadout.addEventListener("click", () => {
      if (!loadout.hidden) selectLoadout(loadout);
    });
  });

  showCharacter();
})();
