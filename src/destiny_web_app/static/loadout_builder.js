(() => {
  const form = document.querySelector('.builder-form');
  if (!form) return;
  const count = form.querySelector('[data-builder-count]');
  const refreshCount = () => {
    const selected = form.querySelectorAll('.builder-choice input:checked').length;
    if (count) count.textContent = `${selected} / 10 slots selected`;
  };
  form.addEventListener('change', refreshCount);
  refreshCount();
  const search = form.querySelector('[data-builder-search]');
  search?.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase();
    form.querySelectorAll('.builder-choice').forEach((choice) => {
      choice.hidden = Boolean(query) && !choice.dataset.search.includes(query);
    });
  });

  const classType = form.querySelector('[name="class_type"]')?.value;
  form.querySelectorAll('[data-bucket]').forEach((bucket) => {
    const grid = bucket.querySelector('[data-bucket-grid]');
    const query = bucket.querySelector('[data-bucket-query]');
    const more = bucket.querySelector('[data-load-more]');
    const result = bucket.querySelector('[data-bucket-result]');
    if (!grid || !query || !more || !result) return;
    grid.dataset.class = classType;
    let timer;
    const load = async ({replace = false} = {}) => {
      more.disabled = true;
      result.textContent = 'Loading…';
      const selected = grid.querySelector('input:checked')?.closest('.builder-choice');
      const selectedValue = selected?.querySelector('input')?.value;
      const offset = replace ? 0 : Number(grid.dataset.offset || 0);
      const params = new URLSearchParams({class: classType, bucket: grid.dataset.bucketHash, offset: String(offset), q: query.value});
      try {
        const response = await fetch(`/loadouts/builder/items?${params}`, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Items could not be loaded.');
        if (replace) grid.innerHTML = payload.html;
        else grid.insertAdjacentHTML('beforeend', payload.html);
        if (selectedValue) {
          const replacement = [...grid.querySelectorAll('input')].find((input) => input.value === selectedValue);
          if (replacement) replacement.checked = true;
          else grid.insertAdjacentHTML('afterbegin', selected.outerHTML);
        }
        grid.dataset.offset = payload.next_offset;
        grid.dataset.total = payload.total;
        more.hidden = !payload.has_more;
        result.textContent = `${Math.min(payload.next_offset, payload.total)} of ${payload.total}`;
        refreshCount();
      } catch (error) {
        result.textContent = error.message;
      } finally {
        more.disabled = false;
      }
    };
    more.addEventListener('click', () => load());
    query.addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => load({replace: true}), 250);
    });
  });
})();
