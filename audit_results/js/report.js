(function () {
  const themeStorageKey = 'jellyfin-library-auditor-theme';
  const themeToggle = document.getElementById('theme-toggle');
  const themeToggleValue = document.getElementById('theme-toggle-value');
  function applyTheme(theme, persist) {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    if (themeToggle) {
      themeToggle.checked = theme === 'dark';
    }
    if (themeToggleValue) {
      themeToggleValue.textContent = theme === 'dark' ? 'Dark' : 'Light';
    }
    if (!persist) { return; }
    try {
      window.localStorage.setItem(themeStorageKey, theme);
    } catch (error) {}
  }
  const currentTheme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
  applyTheme(currentTheme, false);
  themeToggle?.addEventListener('change', () => {
    applyTheme(themeToggle.checked ? 'dark' : 'light', true);
  });
  const root = document.querySelector('[data-nav-current]');
  if (root) {
    document.querySelectorAll('.nav-link[data-nav]').forEach((link) => {
      if (link.dataset.nav === root.dataset.navCurrent) {
        link.classList.add('is-active');
      }
    });
  }
  const searchInput = document.getElementById('report-search');
  function applySearch() {
    document.querySelectorAll('table').forEach((table) => {
      if (table.querySelector('[data-search-row]')) {
        applyReportTableFilters(table);
      }
    });
  }
  searchInput?.addEventListener('input', applySearch);
  applySearch();
})();

function applyReportTableFilters(table) {
  if (!table) { return; }
  const query = document.getElementById('report-search')?.value.trim().toLowerCase() || '';
  const hideSame = table.dataset.hideSame === 'true';
  let visibleCount = 0;
  Array.from(table.tBodies[0]?.rows || []).forEach((row) => {
    const matchesQuery = !row.hasAttribute('data-search-row') || query === '' || row.dataset.search.includes(query);
    const keepWhenHidingSame = !hideSame || row.hasAttribute('data-diff-row') || row.hasAttribute('data-static-row');
    const visible = matchesQuery && keepWhenHidingSame;
    row.hidden = !visible;
    if (visible && !row.hasAttribute('data-static-row')) { visibleCount += 1; }
  });
  updateRowCount(table, visibleCount);
}

function updateRowCount(table, count) {
  const countElement = table.closest('.section-card')?.querySelector('[data-row-count]');
  if (countElement) {
    countElement.textContent = `(${count})`;
  }
}

function toggleSameRows(button) {
  const table = button.closest('.section-card')?.querySelector('table[data-hide-same]');
  if (!table) { return; }
  const hideSame = table.dataset.hideSame === 'true';
  const nextHideSame = !hideSame;
  table.dataset.hideSame = nextHideSame ? 'true' : 'false';
  button.textContent = nextHideSame ? 'Show all' : 'Hide same';
  button.setAttribute('aria-pressed', nextHideSame ? 'true' : 'false');
  applyReportTableFilters(table);
}

function sortReportTable(button) {
  const table = button.closest('table');
  if (!table) { return; }
  const body = table.tBodies[0];
  const columnIndex = Number(button.dataset.column);
  const rows = Array.from(body.rows);
  const ascending = table.dataset.sortColumn !== String(columnIndex) || table.dataset.sortDirection !== 'asc';
  rows.sort((left, right) => {
    const leftValue = getSortValue(left.cells[columnIndex]);
    const rightValue = getSortValue(right.cells[columnIndex]);
    const leftNumber = Number(leftValue);
    const rightNumber = Number(rightValue);
    const bothNumeric = leftValue !== '' && rightValue !== '' && Number.isFinite(leftNumber) && Number.isFinite(rightNumber);
    if (bothNumeric) {
      if (leftNumber < rightNumber) { return ascending ? -1 : 1; }
      if (leftNumber > rightNumber) { return ascending ? 1 : -1; }
      return 0;
    }
    if (leftValue < rightValue) { return ascending ? -1 : 1; }
    if (leftValue > rightValue) { return ascending ? 1 : -1; }
    return 0;
  });
  rows.forEach((row) => body.appendChild(row));
  table.dataset.sortColumn = String(columnIndex);
  table.dataset.sortDirection = ascending ? 'asc' : 'desc';
  applyReportTableFilters(table);
}

function getSortValue(cell) {
  if (!cell) { return ''; }
  const sortValue = cell.dataset.sortValue;
  if (typeof sortValue === 'string') {
    return sortValue.trim().toLowerCase();
  }
  return cell.textContent.trim().toLowerCase();
}

function copyTransferCommand(button) {
  const command = button.dataset.command;
  if (!command) { return; }
  const originalTitle = button.getAttribute('title') || '';
  function showCopied() {
    button.classList.add('is-copied');
    button.setAttribute('title', 'Copied to clipboard');
    window.setTimeout(() => {
      button.classList.remove('is-copied');
      button.setAttribute('title', originalTitle);
    }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(command).then(showCopied, () => copyTransferCommandFallback(command, showCopied));
  } else {
    copyTransferCommandFallback(command, showCopied);
  }
}

function copyTransferCommandFallback(command, onDone) {
  const textarea = document.createElement('textarea');
  textarea.value = command;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try { document.execCommand('copy'); } catch (error) {}
  document.body.removeChild(textarea);
  onDone();
}