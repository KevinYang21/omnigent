"""Android server-picker pill must never leave the sidebar header buttons dead.

The Android shell historically floated a native ``TextView`` pill (the server
switcher) above the WebView, centred over the whole window with no width cap
(``web/android/.../MainActivity.kt``: ``Gravity.TOP or Gravity.CENTER_HORIZONTAL``,
``WRAP_CONTENT``, ``isClickable = true``, added to the ``FrameLayout`` after the
WebView so it is always on top). On a tablet-width viewport (>=768 CSS px) the
conversations sidebar docks open, and its header action cluster
(``data-testid="sidebar-header-actions"``: Search / Settings / Collapse) sits at
the sidebar card's top-right. When the pill's horizontal span
``[W/2 - p, W/2 + p]`` intersects the cluster, every tap on those buttons is
swallowed by the native view - the buttons are dead. At the maximum sidebar
width (0.5 x window) the collision is unconditional for any pill wider than
~24px, i.e. any hostname.

The pill itself is native chrome this browser harness cannot render, so both
tests assert the *web-side contract* that must hold for the buttons to be
reachable on Android, under an injected Android bridge stub (the same
feature-detection stubbing ``test_android_shell.py`` uses). They drive the real
user journey from the report: tablet viewport -> sidebar docked open -> widen
the sidebar to the max via its resize handle -> the header buttons must remain
tappable.

Two shell generations, two contracts:

1. **Legacy shell** (the shipped bridge: no server-picker IPC, no compatibility
   handshake) - the generation that renders the overlapping pill. When the
   pill's window-centred span would cover the header action cluster, the web
   layer must mitigate: drive the pill hidden over the bridge, render its own
   in-web server picker (``data-testid="sidebar-server-picker"``), or refuse to
   run the app under a shell it cannot make safe (an explicit update-required
   page - no interactive sidebar is rendered under the pill at all).
2. **Picker-hosting shell** (a bridge exposing the full server-picker protocol:
   ``nativeBridgeVersion`` / ``nativeWebReady`` / ``nativeHeartbeat`` /
   ``getServerPicker`` / ``switchServer`` / ``openServerSetup``) - server
   selection must live in the web sidebar (the in-web picker renders), so no
   floating native pill is needed over the main surface.

On an unfixed build the legacy case fails: the app renders normally, every
``setNativeServerSwitcherHidden`` call site is ``isIOSShell()``-gated
(``ChatPage.tsx``, ``useNativeServerSwitcher.ts``), the legacy Android bridge
has no hide handler (``OmnigentBridgeListener.kt`` dispatches only
setColorScheme / setBadgeCount / notify), and no in-web picker renders - so the
buttons under the pill are dead.

The pill span is computed from the same inputs the native code uses: the page's
``host[:port]`` label (``hostLabelOf``), 12sp text, 12dp horizontal padding,
25px height at 8px top margin (the constants ``index.css`` mirrors as
``--omnigent-android-switcher-margin/height``), centred at ``window.innerWidth / 2``.
"""

from __future__ import annotations

from playwright.sync_api import Page, ViewportSize, expect

# 768px is the narrowest width where the sidebar docks (Tailwind ``md``,
# ``matchMedia("(min-width: 768px)")`` in AppShell) - the tablet / unfolded
# foldable form factor the report names.
_TABLET_VIEWPORT: ViewportSize = {"width": 768, "height": 1024}

# Legacy Android bridge stub, mirroring the shipped ``NativeBridgeScript``
# shape (no server-picker IPC, no compatibility handshake) the way
# tests/e2e_ui/mobile/test_android_shell.py does. ``setServerSwitcherHidden`` /
# ``setSidebarOpen`` record their calls into ``window.__switcherHideCalls`` so
# the test can observe whether the web layer ever drives the native pill
# hidden.
_LEGACY_ANDROID_INIT_SCRIPT = """
window.__switcherHideCalls = [];
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  setServerSwitcherHidden: function (hidden) {
    window.__switcherHideCalls.push({ method: "setServerSwitcherHidden", hidden: hidden });
  },
  setSidebarOpen: function (hidden) {
    window.__switcherHideCalls.push({ method: "setSidebarOpen", hidden: hidden });
  },
};
"""

# Picker-hosting Android bridge stub: the full server-picker protocol a shell
# that no longer needs the floating pill exposes over ``window.omnigentNative``.
_PICKER_ANDROID_INIT_SCRIPT = """
window.__switcherHideCalls = [];
window.omnigentNative = {
  kind: "android",
  nativeBridgeVersion: 1,
  nativeWebReady: function () {},
  nativeHeartbeat: function () {},
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
  setServerSwitcherHidden: function (hidden) {
    window.__switcherHideCalls.push({ method: "setServerSwitcherHidden", hidden: hidden });
  },
  setSidebarOpen: function (hidden) {
    window.__switcherHideCalls.push({ method: "setSidebarOpen", hidden: hidden });
  },
  getServerPicker: function () {
    return Promise.resolve({
      currentOrigin: window.location.origin,
      recentServers: [],
    });
  },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
};
"""

# Compute the native pill's on-screen rect exactly as the legacy MainActivity
# lays it out: label = host[:port] of the connected server (location.host drops
# default ports, matching hostLabelOf), 12sp text (12 CSS px at density 1)
# measured with Android's UI font stack, 12dp padding each side, centred
# horizontally over the whole window, 8px below the (zero, in a plain browser)
# safe area, 25px tall (--omnigent-android-switcher-height in index.css).
_PILL_RECT = """
() => {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = "12px Roboto, 'Helvetica Neue', Arial, sans-serif";
  const textWidth = ctx.measureText(window.location.host).width;
  const pillWidth = textWidth + 24;
  const centerX = window.innerWidth / 2;
  return {
    left: centerX - pillWidth / 2,
    right: centerX + pillWidth / 2,
    top: 8,
    bottom: 8 + 25,
    label: window.location.host,
    width: pillWidth,
  };
}
"""


def _widen_sidebar_to_clamp(page: Page) -> None:
    """Drive the report's step 2: widen the docked sidebar to its max width.

    The resize handle is a keyboard-operable separator (ArrowRight grows 20px
    per press, clamped at 0.5 x window = 384px here). 320 (default) -> 384
    takes 4 presses; a few extra presses just hit the clamp.
    """
    handle = page.get_by_role("separator", name="Resize sidebar")
    handle.focus()
    for _ in range(6):
        handle.press("ArrowRight")
    sidebar_box = page.locator(".conversations-sidebar").bounding_box()
    assert sidebar_box is not None
    assert sidebar_box["width"] >= 380, (
        f"sidebar did not widen to the viewport clamp (got {sidebar_box['width']}px); "
        "the collision precondition needs the user-widened sidebar from the report"
    )


def _assert_header_actions_escape_the_pill(page: Page) -> None:
    """Assert the web-side mitigation contract at the widened-sidebar state."""
    actions = page.get_by_test_id("sidebar-header-actions")
    pill = page.evaluate(_PILL_RECT)
    cluster = actions.bounding_box()
    assert cluster is not None
    overlap_x = min(pill["right"], cluster["x"] + cluster["width"]) - max(pill["left"], cluster["x"])
    overlap_y = min(pill["bottom"], cluster["y"] + cluster["height"]) - max(pill["top"], cluster["y"])
    collides = overlap_x > 0 and overlap_y > 0

    # The ways a fixed web layer escapes the collision while still running.
    hide_calls = page.evaluate(
        "() => window.__switcherHideCalls.filter((c) => c.hidden === true)"
    )
    web_picker_count = page.get_by_test_id("sidebar-server-picker").count()

    assert not collides or hide_calls or web_picker_count > 0, (
        "Android server-picker pill covers the sidebar header actions and the web "
        "layer never mitigates: the native pill "
        f"('{pill['label']}', {pill['width']:.0f}px wide) spans "
        f"x=[{pill['left']:.0f},{pill['right']:.0f}] y=[{pill['top']},{pill['bottom']}], "
        f"overlapping the Search/Settings/Collapse cluster at "
        f"x=[{cluster['x']:.0f},{cluster['x'] + cluster['width']:.0f}] "
        f"y=[{cluster['y']:.0f},{cluster['y'] + cluster['height']:.0f}] "
        f"by {overlap_x:.0f}x{overlap_y:.0f}px - every tap there lands on the "
        "native view, so the buttons are dead. The web layer neither asked the "
        f"shell to hide the pill (setServerSwitcherHidden(true) calls: {hide_calls}) "
        f"nor rendered its own server picker (found {web_picker_count})."
    )


def test_legacy_android_shell_never_leaves_dead_buttons_under_the_pill(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Under the shipped pill-rendering shell, the web must mitigate or refuse.

    Journey (from the report's steps to reproduce): open the app in the Android
    shell at a tablet-width viewport, where the sidebar docks open by default;
    widen the sidebar via its right-edge resize handle (any user-widened
    sidebar lands the header cluster inside the pill's span, and the maximum
    width collides for every hostname); then the Search / Settings /
    Collapse-sidebar buttons in the sidebar's top-right corner must be
    tappable.

    A build that keeps serving the interactive app to this shell generation
    must hide the pill over the bridge or render its own in-web picker; a build
    that cannot make this shell safe must instead refuse with an explicit
    update-required page (then no interactive sidebar renders under the pill at
    all, so there are no dead buttons). An unfixed build does neither: the app
    renders and the buttons under the pill are dead.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_TABLET_VIEWPORT)
    page.add_init_script(_LEGACY_ANDROID_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # Wait for the web layer to commit to one of its two legitimate states:
    # the app shell, or an explicit shell-incompatibility page.
    outcome = page.locator(".app-shell, #omnigent-native-incompatible")
    expect(outcome.first).to_be_visible()

    if page.locator("#omnigent-native-incompatible").count() > 0:
        # The web refuses to run under a shell whose pill it cannot control -
        # nothing interactive renders beneath the pill, so no taps are lost.
        assert page.get_by_test_id("sidebar-header-actions").count() == 0
        return

    # The app chose to run under the pill-rendering shell: the sidebar docks
    # open at md and the mitigation contract must hold at the widened width.
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    expect(page.get_by_test_id("sidebar-header-actions")).to_be_visible()
    _widen_sidebar_to_clamp(page)
    _assert_header_actions_escape_the_pill(page)


def test_picker_hosting_android_shell_moves_server_selection_into_the_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A shell exposing the picker protocol gets in-sidebar server selection.

    Same journey at the same tablet width, under a bridge that hosts the full
    server-picker protocol. The app must render (no update gate), and server
    selection must not rely on a floating native pill over the main surface:
    either the in-web sidebar picker renders, or the web explicitly drives the
    pill hidden.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_TABLET_VIEWPORT)
    page.add_init_script(_PICKER_ANDROID_INIT_SCRIPT)
    page.goto(f"{base_url}/c/{session_id}")

    # A protocol-complete shell must never be bounced to the update page.
    expect(page.locator(".app-shell")).to_have_attribute("data-android-native", "true")
    expect(page.get_by_test_id("sidebar-header-actions")).to_be_visible()

    _widen_sidebar_to_clamp(page)
    _assert_header_actions_escape_the_pill(page)
