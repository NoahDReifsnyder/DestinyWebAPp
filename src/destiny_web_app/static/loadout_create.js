(() => {
  const form = document.querySelector("[data-import-wizard]");
  if (!form) return;
  const characterPicker = form.querySelector("[data-character-picker]");
  const groups = [...form.querySelectorAll("[data-import-character]")];
  const saveStep = form.querySelector("[data-save-step]");
  const selectedCharacter = form.querySelector("[data-selected-character]");
  const selectedSlot = form.querySelector("[data-selected-slot]");
  const name = form.querySelector("[data-loadout-name]");
  if (!characterPicker) return;

  const showCharacter = () => {
    groups.forEach((group) => {
      group.hidden = group.dataset.importCharacter !== characterPicker.value;
    });
    saveStep.hidden = true;
  };
  characterPicker.addEventListener("change", showCharacter);
  form.addEventListener("click", (event) => {
    const button = event.target.closest("[data-import-slot]");
    if (!button) return;
    form.querySelectorAll("[data-import-slot]").forEach((row) => row.classList.remove("selected"));
    button.classList.add("selected");
    selectedCharacter.value = button.dataset.characterId;
    selectedSlot.value = button.dataset.slotIndex;
    name.value = button.dataset.slotName || `Slot ${Number(button.dataset.slotIndex) + 1}`;
    const icon = form.querySelector(`input[name=cover_icon_hash][value="${button.dataset.iconHash}"]`);
    if (icon) icon.checked = true;
    else if (!form.querySelector("input[name=cover_icon_hash]:checked")) {
      form.querySelector("input[name=cover_icon_hash]")?.click();
    }
    saveStep.hidden = false;
    saveStep.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  showCharacter();
})();
