(() => {
  const root = document.querySelector('[data-operation-id]');
  if (!root) return;
  const id = root.dataset.operationId;
  const terminalStates = new Set(['failed', 'completed', 'paused']);
  let lastStatus = root.dataset.operationStatus || '';
  let terminal = terminalStates.has(lastStatus);
  if (terminal) return;
  const poll = async () => {
    if (terminal || document.hidden) return;
    try {
      const response = await fetch(`/loadout-operations/${encodeURIComponent(id)}/status`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('Progress request failed');
      const state = await response.json();
      root.querySelector('[data-progress-phase]').textContent = state.phase;
      root.querySelector('[data-progress-count]').textContent = `${state.completed} / ${state.total}`;
      root.querySelector('[data-progress-bar]').style.width = `${state.progress_percent}%`;
      root.querySelector('[data-progress-copy]').textContent = `${state.progress_percent}% complete · ${state.attempts} attempt(s) on the current checkpoint.`;
      root.querySelector('.progress-track').setAttribute('aria-valuenow', state.progress_percent);
      if (terminalStates.has(state.status) && !terminalStates.has(lastStatus)) {
        terminal = true;
        window.location.reload();
        return;
      }
      lastStatus = state.status;
    } catch (error) {
      root.querySelector('[data-progress-copy]').textContent = 'Progress polling paused; the durable server operation may still be running. Refresh safely at any time.';
    }
  };
  const timer = window.setInterval(poll, 1200);
  window.addEventListener('beforeunload', () => window.clearInterval(timer));
  poll();
})();
