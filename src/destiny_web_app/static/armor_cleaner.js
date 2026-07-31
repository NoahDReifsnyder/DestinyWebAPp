"use strict";

const organizeForm = document.querySelector("#organize-armor");
const progressPanel = document.querySelector("#organize-progress");
const progressTrack = progressPanel?.querySelector("[role='progressbar']");
const progressFill = document.querySelector("#progress-fill");
const progressMessage = document.querySelector("#progress-message");
const progressCount = document.querySelector("#progress-count");
const progressStage = document.querySelector("#progress-stage");
const progressEta = document.querySelector("#progress-eta");
const policyForm = document.querySelector(".armor-policy");

const statFields = ["weapons", "health", "class", "grenade", "super", "melee"];

organizeForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const confirmed = window.confirm(
    "This will refresh inventory, lock every armor keeper, and unlock every " +
      "review candidate. Equipped and loadout armor is included. Continue?",
  );
  if (!confirmed) return;
  const button = organizeForm.querySelector("button");
  button.disabled = true;
  button.textContent = "Organization running…";
  progressPanel.hidden = false;
  try {
    const response = await fetch(organizeForm.action, {
      method: "POST",
      body: new FormData(organizeForm),
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Organization could not start.");
    }
    await pollProgress();
  } catch (error) {
    progressMessage.textContent = error.message;
    progressStage.textContent = "Stopped";
    progressEta.textContent = "You can safely try again.";
    button.disabled = false;
    button.textContent = "Lock keepers & unlock candidates";
  }
});

async function pollProgress() {
  while (true) {
    const response = await fetch("/cleaner/armor/organize/status", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Progress status could not be loaded.");
    const job = await response.json();
    renderProgress(job);
    if (["complete", "partial", "failed"].includes(job.status)) {
      const key = job.status === "complete" ? "notice" : "error";
      const message = job.error || job.message;
      window.location.assign(`/cleaner/armor?${key}=${encodeURIComponent(message)}`);
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
}

function renderProgress(job) {
  const total = Number(job.total || 0);
  const completed = Number(job.completed || 0);
  const percent = total ? Math.min(100, (completed / total) * 100) : 0;
  progressMessage.textContent = job.message;
  progressCount.textContent = total ? `${completed} / ${total}` : "Preparing";
  progressFill.style.width = `${percent}%`;
  progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
  progressStage.textContent = job.status === "retrying"
    ? `Retry ${job.retry_completed} / ${job.retry_total}`
    : stageLabel(job.status);
  if (completed > 0 && completed < total && job.status === "running") {
    const remaining = (job.elapsed_seconds / completed) * (total - completed);
    progressEta.textContent = `About ${formatDuration(remaining)} remaining`;
  } else if (job.status === "running") {
    progressEta.textContent = "Estimating time remaining…";
  } else {
    progressEta.textContent = stageLabel(job.status);
  }
}

function stageLabel(status) {
  return ({
    preparing: "Preparing",
    running: "Applying states",
    verifying: "Verifying",
    retrying: "Retrying",
    complete: "Complete",
    partial: "Needs attention",
    failed: "Stopped",
  })[status] || status;
}

function formatDuration(seconds) {
  const rounded = Math.max(1, Math.round(seconds));
  if (rounded < 60) return `${rounded} seconds`;
  return `${Math.floor(rounded / 60)}m ${rounded % 60}s`;
}

async function resumeActiveProgress() {
  try {
    const response = await fetch("/cleaner/armor/organize/status", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) return;
    const job = await response.json();
    if (["complete", "partial", "failed"].includes(job.status)) return;
    progressPanel.hidden = false;
    const button = organizeForm.querySelector("button");
    button.disabled = true;
    button.textContent = "Organization running…";
    renderProgress(job);
    await pollProgress();
  } catch (_error) {
    // The page remains usable if a transient progress request fails.
  }
}

resumeActiveProgress();

if (policyForm) {
  const globalInterested = policyForm.querySelector("input[name='global_interested']");
  const globalIncludeRaid = policyForm.querySelector("input[name='global_include_raid']");
  const globalStats = statFields
    .map((field) => policyForm.querySelector(`input[name='global_stats_${field}']`))
    .filter(Boolean);
  const setCards = [...policyForm.querySelectorAll(".set-policy")];

  const syncSetCard = (card) => {
    const prefix = card.dataset.prefix;
    if (!prefix) return;
    const customToggle = card.querySelector("[data-role='set-custom-toggle']");
    const controls = card.querySelector("[data-role='set-advanced-controls']");
    if (!customToggle || !controls) return;

    const custom = customToggle.checked;
    controls.querySelectorAll("input, select").forEach((control) => {
      control.disabled = !custom;
    });
    card.classList.toggle("set-follows-simple", !custom);

    if (custom) return;

    const interested = controls.querySelector(`input[name='${prefix}_interested']`);
    const isRaid = card.dataset.raid === "1";
    const raidAllowed = !isRaid || !globalIncludeRaid || globalIncludeRaid.checked;

    if (interested && globalInterested) {
      interested.checked = globalInterested.checked && raidAllowed;
    }

    ["primary", "secondary", "tertiary"].forEach((position) => {
      statFields.forEach((field, index) => {
        const setStat = controls.querySelector(
          `input[name='${prefix}_${position}_${field}']`,
        );
        const globalStat = globalStats[index];
        if (setStat && globalStat) {
          setStat.checked = globalStat.checked;
        }
      });
    });
  };

  const syncAllCards = () => {
    setCards.forEach(syncSetCard);
  };

  policyForm
    .querySelectorAll(
      "input[name='global_interested'], input[name='global_include_raid'], " +
        "input[name^='global_stats_']",
    )
    .forEach((input) => {
      input.addEventListener("change", syncAllCards);
    });

  setCards.forEach((card) => {
    const customToggle = card.querySelector("[data-role='set-custom-toggle']");
    if (!customToggle) return;
    customToggle.addEventListener("change", () => syncSetCard(card));
  });

  syncAllCards();
}
