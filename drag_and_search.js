// Dispatcharr's multi-select can't be sorted or searched, so this adds both on top of it.
// If an update breaks it, the field still works by scrolling, and by removing and
// re-picking a service.
(() => {
  const LABELS = ['Select Streaming Services to Use + Priority', 'Allowed countries', 'Allowed cities'];
  const SEARCHABLE = ['Select Streaming Services to Use + Priority'];
  const PILL = '.mantine-Pill-root';
  const MARK = '#4dabf7';

  // Where a drop at (x, y) lands: before or after the nearest box, in any row.
  const dropIndex = (rects, x, y) => {
    let best = 0;
    let bestDistance = Infinity;
    rects.forEach((r, i) => {
      const dx = Math.max(r.left - x, 0, x - r.right);
      const dy = Math.max(r.top - y, 0, y - r.bottom);
      const distance = dy * 1000 + dx; // the row first, then along it
      if (distance < bestDistance) {
        bestDistance = distance;
        best = i;
      }
    });
    return best + (x > (rects[best].left + rects[best].right) / 2 ? 1 : 0);
  };

  const moved = (list, from, to) => {
    const next = [...list];
    const [item] = next.splice(from, 1);
    next.splice(to > from ? to - 1 : to, 0, item);
    return next;
  };

  const matches = (text, query) => text.toLowerCase().includes(query.trim().toLowerCase());

  if (typeof document === 'undefined') {
    module.exports = { dropIndex, moved, matches };
    return;
  }

  if (window.cleanTitlesDrag) return;
  window.cleanTitlesDrag = true;

  // The field's own value and change handler, held by React on the element's parents.
  const control = (el) => {
    const key = Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
    for (let fiber = key && el[key]; fiber; fiber = fiber.return) {
      const props = fiber.memoizedProps;
      if (props && Array.isArray(props.value) && Array.isArray(props.data) && typeof props.onChange === 'function') {
        return props;
      }
    }
    return null;
  };

  const fieldOf = (pill) => {
    const root = pill.closest('.mantine-InputWrapper-root');
    const label = root && root.querySelector('label');
    return label && LABELS.includes(label.textContent.replace('*', '').trim()) ? root : null;
  };

  // The search box is the field's own input: Dispatcharr keeps it read-only, but it already
  // holds typed text and clears it when a service is picked or the field loses focus.
  const searchInputs = () => [...document.querySelectorAll('.mantine-InputWrapper-root label')]
    .filter((label) => SEARCHABLE.includes(label.textContent.replace('*', '').trim()))
    .map((label) => label.closest('.mantine-InputWrapper-root').querySelector('input:not([type="hidden"])'))
    .filter(Boolean);

  const listOf = (input) => document.querySelector(`[aria-labelledby="${input.id}-label"]`);
  const isOpen = (list) => Boolean(list && list.offsetParent);
  const optionsOf = (list) => [...list.querySelectorAll('[role="option"]')];

  const filter = (input) => {
    const list = listOf(input);
    if (!list) return;
    optionsOf(list).forEach((option) => {
      option.style.display = matches(option.textContent, input.value) ? '' : 'none';
    });
  };

  const paint = () => {
    document.querySelectorAll(PILL).forEach((pill) => {
      if (fieldOf(pill)) pill.style.cursor = 'grab';
    });
    searchInputs().forEach((input) => {
      input.readOnly = false;
      input.style.cursor = 'text';
      filter(input);
    });
  };

  let drag = null;
  let swallowClick = false;

  const clearMark = () => drag.pills.forEach((p) => { p.style.boxShadow = ''; });

  document.addEventListener('mousedown', (event) => {
    const pill = event.button === 0 && event.target.closest(PILL);
    if (!pill || event.target.closest('button') || !fieldOf(pill)) return;
    const pills = [...fieldOf(pill).querySelectorAll(PILL)];
    const props = control(pill);
    if (!props || props.value.length !== pills.length) return;
    // Mantine would otherwise cancel the drag and open the list.
    event.preventDefault();
    event.stopPropagation();
    drag = { pill, pills, props, from: pills.indexOf(pill), to: null, ghost: null };
  }, true);

  document.addEventListener('mousemove', (event) => {
    if (!drag) return;
    if (!drag.ghost) {
      drag.ghost = drag.pill.cloneNode(true);
      Object.assign(drag.ghost.style, { position: 'fixed', zIndex: 9999, pointerEvents: 'none', opacity: 0.85, cursor: 'grabbing', margin: 0 });
      document.body.append(drag.ghost);
      drag.pill.style.opacity = 0.35;
      document.documentElement.style.cursor = 'grabbing';
    }
    drag.ghost.style.left = `${event.clientX + 8}px`;
    drag.ghost.style.top = `${event.clientY + 8}px`;
    drag.to = dropIndex(drag.pills.map((p) => p.getBoundingClientRect()), event.clientX, event.clientY);
    clearMark();
    const before = drag.pills[drag.to];
    if (before) before.style.boxShadow = `-3px 0 0 ${MARK}`;
    else drag.pills[drag.pills.length - 1].style.boxShadow = `3px 0 0 ${MARK}`;
  }, true);

  document.addEventListener('mouseup', () => {
    if (!drag) return;
    const { props, from, to, ghost, pill } = drag;
    clearMark();
    pill.style.opacity = '';
    document.documentElement.style.cursor = '';
    drag = null;
    if (!ghost) return;
    ghost.remove();
    swallowClick = true;
    if (to !== null && to !== from && to !== from + 1) props.onChange(moved(props.value, from, to));
  }, true);

  document.addEventListener('click', (event) => {
    if (!swallowClick) return;
    swallowClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);

  document.addEventListener('input', (event) => {
    const input = event.target;
    if (!searchInputs().includes(input)) return;
    if (!isOpen(listOf(input))) input.click();
    setTimeout(() => filter(input));
  }, true);

  // Enter picks the first service still showing, not the first one in the full list.
  document.addEventListener('keydown', (event) => {
    const input = event.target;
    if (event.key !== 'Enter' || !input.value || !searchInputs().includes(input)) return;
    const first = optionsOf(listOf(input) || document.body).find((option) => option.style.display !== 'none');
    event.preventDefault();
    event.stopPropagation();
    if (first) first.click();
  }, true);

  new MutationObserver(paint).observe(document.body, { childList: true, subtree: true });
  paint();
})();
