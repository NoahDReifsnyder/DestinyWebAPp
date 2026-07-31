(() => {
  const search = document.querySelector("#inventory-search");
  const location = document.querySelector("#location-filter");
  const rarity = document.querySelector("#rarity-filter");
  const owner = document.querySelector("#owner-filter");
  const bucket = document.querySelector("#bucket-filter");
  const itemType = document.querySelector("#type-filter");
  const locked = document.querySelector("#locked-filter");
  const clear = document.querySelector("#clear-filters");
  const result = document.querySelector("#filter-result");
  const tiles = [...document.querySelectorAll(".item-tile")];
  const dialog = document.querySelector("#item-dialog");
  const detail = document.querySelector("#item-detail");
  let detailRequest;

  const populateFilter = (select, dataKey, labelKey = dataKey) => {
    if (!select) return;
    const choices = new Map();
    for (const tile of tiles) {
      const value = tile.dataset[dataKey];
      if (value && !choices.has(value)) {
        choices.set(value, tile.dataset[labelKey] || value);
      }
    }
    const options = [...choices.entries()]
      .sort((left, right) => left[1].localeCompare(right[1]));
    for (const [value, label] of options) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.append(option);
    }
  };

  populateFilter(rarity, "rarity", "rarityLabel");
  populateFilter(owner, "owner", "ownerLabel");
  populateFilter(bucket, "bucket");
  populateFilter(itemType, "itemType");

  const applyFilters = () => {
    const query = search?.value.trim().toLowerCase() ?? "";
    const locationValue = location?.value ?? "";
    const rarityValue = rarity?.value ?? "";
    const ownerValue = owner?.value ?? "";
    const bucketValue = bucket?.value ?? "";
    const typeValue = itemType?.value ?? "";
    const lockedOnly = locked?.checked ?? false;
    let visible = 0;

    for (const tile of tiles) {
      const matches =
        (!query || tile.dataset.search.includes(query)) &&
        (!locationValue || tile.dataset.location === locationValue) &&
        (!rarityValue || tile.dataset.rarity === rarityValue) &&
        (!ownerValue || tile.dataset.owner === ownerValue) &&
        (!bucketValue || tile.dataset.bucket === bucketValue) &&
        (!typeValue || tile.dataset.itemType === typeValue) &&
        (!lockedOnly || tile.dataset.locked === "yes");
      tile.hidden = !matches;
      if (matches) visible += 1;
    }

    for (const group of document.querySelectorAll(".bucket-group")) {
      const hasVisible = [...group.querySelectorAll(".item-tile")]
        .some((tile) => !tile.hidden);
      group.hidden = !hasVisible;
      if (hasVisible && (
        query || locationValue || rarityValue || ownerValue ||
        bucketValue || typeValue || lockedOnly
      )) {
        group.open = true;
      }
    }

    for (const zone of document.querySelectorAll(".inventory-zone")) {
      const hasVisible = [...zone.querySelectorAll(".item-tile")]
        .some((tile) => !tile.hidden);
      zone.hidden = tiles.length > 0 && !hasVisible;
    }

    if (result) {
      const filtering = (
        query || locationValue || rarityValue || ownerValue ||
        bucketValue || typeValue || lockedOnly
      );
      result.style.display = filtering ? "block" : "none";
      result.textContent = `${visible.toLocaleString()} of ${tiles.length.toLocaleString()} items`;
    }
  };

  for (const control of [
    search, location, rarity, owner, bucket, itemType, locked,
  ]) {
    control?.addEventListener("input", applyFilters);
    control?.addEventListener("change", applyFilters);
  }

  clear?.addEventListener("click", () => {
    search.value = "";
    location.value = "";
    rarity.value = "";
    owner.value = "";
    bucket.value = "";
    itemType.value = "";
    locked.checked = false;
    applyFilters();
    search.focus();
  });

  document.addEventListener("keydown", (event) => {
    const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(
      document.activeElement?.tagName,
    );
    if (event.key === "/" && !editing) {
      event.preventDefault();
      search?.focus();
    }
    if (event.key === "Escape" && dialog?.open) dialog.close();
  });

  const escapeHtml = (value) => {
    const node = document.createElement("span");
    node.textContent = value ?? "";
    return node.innerHTML;
  };

  const chips = (rows, emptyText) => {
    if (!rows?.length) return `<span>${escapeHtml(emptyText)}</span>`;
    return rows.map((row) => {
      const status = row.status
        ? `<small>${escapeHtml(row.status)}</small>`
        : "";
      const className = row.status ? ' class="is-inactive"' : "";
      return `<span${className}>${escapeHtml(row.name)}${status}</span>`;
    }).join("");
  };

  const renderDetail = (item) => {
    const badges = [
      item.tier,
      item.location,
      item.locked ? "Locked" : null,
      item.masterworked ? "Masterworked" : null,
      item.crafted ? "Crafted" : null,
    ].filter(Boolean).map((label) => `<span>${escapeHtml(label)}</span>`).join("");
    const stats = item.stats?.length
      ? item.stats.map((stat) => {
          const width = Math.min(100, Math.max(0, Number(stat.value) || 0));
          return `<div class="stat-row"><span>${escapeHtml(stat.name)}</span>
            <span class="stat-bar"><i style="width:${width}%"></i></span>
            <b>${escapeHtml(stat.value)}</b></div>`;
        }).join("")
      : '<p class="muted">No instance stats stored for this item.</p>';
    const icon = item.icon
      ? '<img class="detail-icon" data-detail-icon alt="">'
      : '<span class="detail-icon missing-icon">◇</span>';
    const watermark = item.watermark
      ? '<img class="detail-watermark" data-detail-watermark alt="">'
      : "";

    return `<header class="detail-hero">
        ${watermark}
        ${icon}
        <div class="detail-heading">
          <p class="eyebrow">${escapeHtml(item.tier)}</p>
          <h2>${escapeHtml(item.name)}</h2>
          <p>${escapeHtml(item.type)}</p>
          <p class="detail-power">✦ ${escapeHtml(item.power ?? "—")}</p>
        </div>
      </header>
      <div class="detail-body">
        ${item.description ? `<p class="detail-description">${escapeHtml(item.description)}</p>` : ""}
        <div class="detail-badges">${badges}</div>
        <section class="detail-section"><h3>Stats</h3>${stats}</section>
        <section class="detail-section"><h3>Sockets</h3>
          <div class="chip-list">${chips(item.sockets, "No visible socket data")}</div>
        </section>
        <section class="detail-section"><h3>Perks</h3>
          <div class="chip-list">${chips(item.perks, "No visible perk data")}</div>
        </section>
        <section class="detail-section"><h3>Stored record</h3>
          <dl class="provenance">
            <dt>Quantity</dt><dd>${escapeHtml(item.quantity)}</dd>
            <dt>Location</dt><dd>${escapeHtml(item.location)}</dd>
            <dt>Instance</dt><dd>${escapeHtml(item.instance_id ?? "Not instanced")}</dd>
            <dt>First seen</dt><dd>${escapeHtml(item.first_seen_at)}</dd>
            <dt>Last seen</dt><dd>${escapeHtml(item.last_seen_at)}</dd>
          </dl>
        </section>
      </div>`;
  };

  document.addEventListener("click", async (event) => {
    if (!(event.target instanceof Element)) return;
    const tile = event.target.closest(".item-tile");
    if (!tile || !dialog || !detail) return;
    detailRequest?.abort();
    detailRequest = new AbortController();
    detail.innerHTML = '<div class="drawer-loading">Loading item details…</div>';
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(`/inventory/item/${tile.dataset.itemId}`, {
        headers: { Accept: "application/json" },
        signal: detailRequest.signal,
      });
      if (!response.ok) throw new Error("Item details could not be loaded.");
      const item = await response.json();
      detail.innerHTML = renderDetail(item);
      const icon = detail.querySelector("[data-detail-icon]");
      const watermark = detail.querySelector("[data-detail-watermark]");
      if (icon && item.icon) icon.src = item.icon;
      if (watermark && item.watermark) watermark.src = item.watermark;
    } catch (error) {
      if (error.name === "AbortError") return;
      detail.innerHTML = `<div class="drawer-error">${escapeHtml(error.message)}</div>`;
    }
  });

  document.querySelector(".dialog-close")?.addEventListener("click", () => {
    detailRequest?.abort();
    dialog.close();
  });
  dialog?.addEventListener("click", (event) => {
    if (event.target === dialog) {
      detailRequest?.abort();
      dialog.close();
    }
  });

  for (const form of document.querySelectorAll('form[action="/inventory/manifest/sync"]')) {
    form.addEventListener("submit", () => {
      const wait = document.querySelector("#manifest-wait");
      if (wait) wait.hidden = false;
    });
  }
})();
