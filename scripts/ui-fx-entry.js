// Source of gateway/ui/fx.js, built by scripts/vendor-ui.sh. The console is plain JS; only this effect runs on React.
// MetalFx (metal-fx, from libraries.dev) draws an animated liquid-metal ring around a primary button.
import { createElement as e } from "react";
import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import { MetalFx } from "metal-fx";

const roots = new Map();

/** Wrap `button` (already inside `host`) in the metal ring. theme: "light" | "dark". */
export function metal(host, button, theme, paused) {
  if (!host.isConnected || roots.has(host)) return;
  // Unmount buttons of replaced pages only after this one is up: metal-fx shares one WebGL engine and tears it down
  // when its last instance unmounts; a ring mounted onto a freshly rebuilt engine never showed.
  setTimeout(() => { for (const [old, r] of roots) if (!old.isConnected) { r.unmount(); roots.delete(old); } }, 0);
  const radius = parseFloat(getComputedStyle(button).borderTopLeftRadius) || 0;
  const slot = e("div", { ref: (n) => n && button.parentNode !== n && n.append(button), style: { display: "inline-flex", width: "100%" } });
  const root = createRoot(host);
  roots.set(host, root);
  button.remove();
  try {
    flushSync(() => root.render(e(MetalFx, { variant: "button", preset: "silver", theme, borderRadius: radius, paused }, slot)));
  } catch (err) { // never lose the button: put the plain one back
    restore(host, button);
    throw err;
  }
  // metal-fx keeps the whole button (label included) hidden until its first WebGL frame. If that frame never comes
  // (GPU blocked, context lost), the button would stay invisible, so put the plain one back.
  setTimeout(() => {
    const ring = host.querySelector(".metal-fx-root");
    if (host.isConnected && roots.get(host) === root && (!ring || getComputedStyle(ring).visibility === "hidden")) restore(host, button);
  }, 1200);
}

function restore(host, button) {
  roots.get(host)?.unmount();
  roots.delete(host);
  host.replaceChildren(button);
}
