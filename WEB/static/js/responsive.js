/** Presentation only: reuse the configuration controls in a compact modal drawer.
 * Moving the original node preserves every input value and existing event handler.
 */
document.addEventListener('DOMContentLoaded', () => {
  const panel = document.getElementById('configuration-panel');
  const drawer = document.getElementById('configuration-drawer');
  const drawerBody = document.getElementById('configuration-drawer-body');
  const toggle = document.getElementById('configuration-toggle');
  const closeButton = document.getElementById('configuration-close');
  const compact = window.matchMedia('(max-width: 1100px)');
  const home = document.createComment('Desktop configuration panel position');
  panel.before(home);
  let scrollPosition = null;

  function unlockPage() {
    if (scrollPosition === null) return;
    const {x, y} = scrollPosition;
    document.documentElement.classList.remove('configuration-open');
    document.documentElement.style.removeProperty('--locked-scroll-top');
    scrollPosition = null;
    window.scrollTo(x, y);
  }

  function closeDrawer() {
    if (!drawer.open) return;
    drawer.close();
    toggle.setAttribute('aria-expanded', 'false');
    unlockPage();
    if (compact.matches) toggle.focus({preventScroll: true});
  }

  function updateLayout() {
    if (compact.matches) {
      drawerBody.append(panel);
    } else {
      closeDrawer();
      home.after(panel);
    }
  }

  toggle.addEventListener('click', () => {
    if (!compact.matches || drawer.open) return;
    scrollPosition = {x: window.scrollX, y: window.scrollY};
    document.documentElement.style.setProperty('--locked-scroll-top', `${-scrollPosition.y}px`);
    document.documentElement.classList.add('configuration-open');
    drawer.showModal();
    toggle.setAttribute('aria-expanded', 'true');
    closeButton.focus();
  });
  closeButton.addEventListener('click', closeDrawer);
  drawer.addEventListener('close', () => {
    // A queued close event may arrive after the user has reopened the drawer.
    if (drawer.open) return;
    toggle.setAttribute('aria-expanded', 'false');
    unlockPage();
    if (compact.matches) toggle.focus({preventScroll: true});
  });
  // The native dialog supplies Escape dismissal and makes background content inert.
  drawer.addEventListener('click', (event) => {
    if (event.target !== drawer) return;
    const bounds = drawer.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) closeDrawer();
  });
  ['btn-run-grid', 'btn-run-solarnex', 'nav-grid-bess', 'nav-solarnex'].forEach(id => {
    document.getElementById(id).addEventListener('click', closeDrawer);
  });
  compact.addEventListener('change', updateLayout);
  updateLayout();
});
