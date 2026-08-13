// Cursor force field for the ambient effects. Pure maths + a tiny tracker, no canvas and
// no settings, so the force law and the spring can be tested outside the browser.
//
// THE PROBLEM THIS SOLVES. The effects keep their state in three incompatible shapes:
// some integrate a velocity every frame (code rain, constellations, flybys), some cache
// static coordinates that nothing ever moves (radar blips, circuit vertices), and some
// hold no per-element state at all because every position is a closed-form function of
// time (aurora, the solar system). Only the first kind can simply be handed a force.
//
// So the shared primitive is a DISPLACEMENT, not a velocity: each element gets an offset
// (ox, oy) that the cursor pushes around and a spring pulls back to zero, and the effect
// draws at its own position plus that offset. An effect keeps computing exactly what it
// computed before — the offset rides on top and decays back to nothing, so an effect that
// is off, or a cursor that has left, always returns to the original picture.
(function (root) {
  // Live pointer state. `idle` counts seconds since the last real move: effects fade the
  // force out rather than freezing a stale one under a cursor that has stopped or left.
  const Pointer = { x: -1e5, y: -1e5, vx: 0, vy: 0, idle: 99, inside: false };

  function trackPointer(target) {
    const el = target || (typeof window !== 'undefined' ? window : null);
    if (!el || !el.addEventListener) return Pointer;
    let lx = null, ly = null;
    el.addEventListener('pointermove', e => {
      if (lx !== null) { Pointer.vx = e.clientX - lx; Pointer.vy = e.clientY - ly; }
      lx = Pointer.x = e.clientX; ly = Pointer.y = e.clientY;
      Pointer.idle = 0; Pointer.inside = true;
    }, { passive: true });
    // leaving the window must release whatever the cursor was holding, or the force stays
    // parked at the last edge position for as long as the page is open
    el.addEventListener('pointerout', e => { if (!e.relatedTarget) Pointer.inside = false; }, { passive: true });
    el.addEventListener('blur', () => { Pointer.inside = false; }, { passive: true });
    return Pointer;
  }

  // Ages the pointer by dt seconds. Velocity decays quickly so 'drag' does not keep
  // flinging things after the cursor stops; idle grows so callers can fade the field.
  function agePointer(dt, p) {
    const P = p || Pointer;
    P.idle += dt;
    const k = Math.exp(-dt * 9);
    P.vx *= k; P.vy *= k;
    return P;
  }

  // Force the cursor exerts on a point, as {fx, fy}. Zero outside `radius`, and smoothly
  // zero AT the radius (falloff^2), so an element crossing the boundary does not jerk.
  //   push  drive it away          pull  draw it in
  //   swirl orbit around the cursor      drag  carry it along with the cursor's motion
  // The 1/(0.25+d) term keeps push/pull finite at the centre: a bare inverse-square would
  // fling anything the cursor passes directly over straight off the screen.
  function pointerForce(px, py, x, y, radius, mode, strength) {
    if (!(radius > 0) || !strength) return { fx: 0, fy: 0 };
    const dx = x - px, dy = y - py;
    const d2 = dx * dx + dy * dy;
    if (d2 > radius * radius) return { fx: 0, fy: 0 };
    const d = Math.sqrt(d2);
    const falloff = 1 - d / radius;
    const w = falloff * falloff * strength;
    if (mode === 'drag') return { fx: 0, fy: 0, drag: w };   // caller uses pointer velocity
    if (mode === 'swirl') {
      const inv = d > 0.001 ? 1 / d : 0;
      return { fx: -dy * inv * w, fy: dx * inv * w };        // perpendicular => orbit
    }
    const inv = d > 0.001 ? 1 / d : 0;
    const s = (mode === 'pull' ? -1 : 1) * w / (0.25 + d / radius);
    return { fx: dx * inv * s, fy: dy * inv * s };
  }

  // One spring step on an element's displacement. The element needs ox/oy/dvx/dvy; they
  // are created on first use so no effect has to pre-seed them.
  //   stiff  how hard it is pulled home (higher = snappier, less lingering)
  //   damp   velocity retained per frame (lower = settles sooner)
  // Returns the element, so it can be used inline.
  function springStep(el, fx, fy, stiff, damp, maxOff) {
    const ox = el.ox || 0, oy = el.oy || 0;
    let vx = (el.dvx || 0) + fx - ox * stiff;
    let vy = (el.dvy || 0) + fy - oy * stiff;
    vx *= damp; vy *= damp;
    let nx = ox + vx, ny = oy + vy;
    if (maxOff > 0) {                       // a hard leash: nothing is ever flung to infinity
      const m = Math.sqrt(nx * nx + ny * ny);
      if (m > maxOff) { const k = maxOff / m; nx *= k; ny *= k; vx *= k; vy *= k; }
    }
    el.ox = nx; el.oy = ny; el.dvx = vx; el.dvy = vy;
    return el;
  }

  const api = { Pointer, trackPointer, agePointer, pointerForce, springStep };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
