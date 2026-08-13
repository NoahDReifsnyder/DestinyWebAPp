(() => {
  const editor = document.querySelector("[data-set-editor]");
  if (!editor) return;
  const cells = [...editor.querySelectorAll("[data-set-cell]")];
  const status = editor.querySelector("[data-draft-status]");
  let selected = null;
  let drag = null;

  const markChanged = () => { status.textContent = "Unsaved board changes"; status.classList.add("changed"); };
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[char]);
  const iconMarkup = (id, name, path) => path
    ? `<span class="draft-icon"><img src="https://www.bungie.net${escapeHtml(path)}" alt=""><span class="sr-only">${escapeHtml(name)}</span></span>`
    : `<span class="draft-icon icon-fallback">◇<span class="sr-only">${escapeHtml(name)}</span></span>`;
  const setCell = (cell, item) => {
    cell.querySelector("[data-slot-value]").value = item?.id || "";
    cell.querySelector(".board-loadout-link, .draft-icon")?.remove();
    cell.querySelector("[data-cell-clear]")?.remove();
    cell.querySelector("[data-cell-select]")?.remove();
    if (item) {
      cell.classList.replace("empty", "occupied");
      cell.insertAdjacentHTML("beforeend", iconMarkup(item.id, item.name, item.path));
      cell.insertAdjacentHTML("beforeend", '<button type="button" class="cell-select" data-cell-select aria-label="Select this position to move or swap">↔</button>');
      cell.insertAdjacentHTML("beforeend", '<button type="button" class="cell-clear" data-cell-clear aria-label="Empty this position">×</button>');
      cell.draggable = true;
    } else {
      cell.classList.replace("occupied", "empty");
      if (!cell.querySelector(".empty-plus")) cell.insertAdjacentHTML("beforeend", '<span class="empty-plus">+</span>');
      cell.draggable = false;
    }
    cell.querySelector(".empty-plus")?.toggleAttribute("hidden", Boolean(item));
  };
  const cellItem = (cell) => {
    const id = cell.querySelector("[data-slot-value]").value;
    if (!id) return null;
    const image = cell.querySelector("img");
    return { id, name: cell.querySelector("a")?.title?.split(" · ")[0] || "Loadout", path: image?.src.replace("https://www.bungie.net", "") || "" };
  };
  const place = (cell, item) => { setCell(cell, item); markChanged(); };
  const clearSelection = () => {
    editor.querySelectorAll(".selected-source").forEach((row) => row.classList.remove("selected-source"));
    selected = null;
  };
  const applySelected = (cell) => {
    if (!selected) return;
    if (selected.kind === "tray") place(cell, selected.item);
    else if (selected.cell !== cell) {
      const targetItem = cellItem(cell);
      setCell(cell, selected.item); setCell(selected.cell, targetItem); markChanged();
    }
    clearSelection();
  };

  editor.addEventListener("click", (event) => {
    const tray = event.target.closest("[data-tray-loadout]");
    if (tray) {
      clearSelection();
      tray.classList.add("selected-source");
      selected = { kind: "tray", item: { id: tray.dataset.loadoutId, name: tray.dataset.loadoutName, path: tray.dataset.iconPath } };
      return;
    }
    const selectCell = event.target.closest("[data-cell-select]");
    if (selectCell) {
      const cell = selectCell.closest("[data-set-cell]");
      clearSelection();
      cell.classList.add("selected-source");
      selected = { kind: "cell", cell, item: cellItem(cell) };
      return;
    }
    const clear = event.target.closest("[data-cell-clear]");
    if (clear) { place(clear.closest("[data-set-cell]"), null); clearSelection(); return; }
    const cell = event.target.closest("[data-set-cell]");
    if (cell && selected && !event.target.closest("a, button")) applySelected(cell);
  });
  editor.addEventListener("keydown", (event) => {
    const cell = event.target.closest("[data-set-cell]");
    if (!cell || event.target !== cell || !selected || !["Enter", " "].includes(event.key)) return;
    event.preventDefault(); applySelected(cell);
  });
  editor.addEventListener("dragstart", (event) => {
    const tray = event.target.closest("[data-tray-loadout]");
    const cell = event.target.closest("[data-set-cell]");
    drag = tray
      ? { kind: "tray", item: { id: tray.dataset.loadoutId, name: tray.dataset.loadoutName, path: tray.dataset.iconPath } }
      : cell ? { kind: "cell", cell, item: cellItem(cell) } : null;
  });
  editor.addEventListener("dragover", (event) => { if (event.target.closest("[data-set-cell]")) event.preventDefault(); });
  editor.addEventListener("drop", (event) => {
    const target = event.target.closest("[data-set-cell]");
    if (!target || !drag?.item) return;
    event.preventDefault();
    if (drag.kind === "tray") place(target, drag.item);
    else if (drag.cell !== target) {
      const targetItem = cellItem(target);
      setCell(target, drag.item); setCell(drag.cell, targetItem); markChanged();
    }
    drag = null;
  });
})();
