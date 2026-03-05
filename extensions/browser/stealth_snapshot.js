// Enumerate interactive elements and assign @e1, @e2... refs.
// Injected into the page by stealth_server.py.
// Returns JSON: { refs: { "e1": { selector, role, name, ... }, ... }, text: "snapshot text" }
(() => {
  const INTERACTIVE = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
    '[role="combobox"]', '[role="listbox"]', '[role="option"]',
    '[role="tab"]', '[role="menuitem"]', '[role="switch"]',
    '[contenteditable="true"]',
  ];

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };

  const getRole = (el) => {
    if (el.getAttribute('role')) return el.getAttribute('role');
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || (tag === 'input' && el.type === 'submit')) return 'button';
    if (tag === 'input') {
      const t = el.type || 'text';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    return tag;
  };

  const getName = (el) => {
    // aria-label > label[for] > placeholder > title > textContent (trimmed)
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) {
      const label = document.querySelector(`label[for="${el.id}"]`);
      if (label) return label.textContent.trim();
    }
    if (el.placeholder) return el.placeholder;
    if (el.title) return el.title;
    const text = el.textContent || '';
    return text.trim().substring(0, 80);
  };

  const getState = (el) => {
    const parts = [];
    if (el.checked) parts.push('checked');
    if (el.disabled) parts.push('disabled');
    if (el.getAttribute('aria-expanded') === 'true') parts.push('expanded');
    if (el.getAttribute('aria-selected') === 'true') parts.push('selected');
    return parts;
  };

  const seen = new Set();
  const refs = {};
  const lines = [];
  let idx = 1;

  const allElements = document.querySelectorAll(INTERACTIVE.join(','));
  for (const el of allElements) {
    if (seen.has(el) || !isVisible(el)) continue;
    seen.add(el);

    const refId = `e${idx}`;
    const role = getRole(el);
    const name = getName(el);
    const state = getState(el);

    // Build a unique CSS selector for this element
    let selector;
    if (el.id) {
      selector = `#${CSS.escape(el.id)}`;
    } else {
      // nth-of-type based path
      const path = [];
      let cur = el;
      while (cur && cur !== document.body) {
        const parent = cur.parentElement;
        if (!parent) break;
        const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
        if (siblings.length > 1) {
          const nth = siblings.indexOf(cur) + 1;
          path.unshift(`${cur.tagName.toLowerCase()}:nth-of-type(${nth})`);
        } else {
          path.unshift(cur.tagName.toLowerCase());
        }
        cur = parent;
      }
      selector = path.join(' > ');
    }

    refs[refId] = { selector, role, name };

    // Format line like agent-browser
    let line = `- ${role}`;
    if (name) line += ` "${name}"`;
    line += ` [ref=${refId}]`;
    if (state.length) line += ` [${state.join('] [')}]`;
    lines.push(line);

    idx++;
  }

  return JSON.stringify({ refs, text: lines.join('\n') });
})();
