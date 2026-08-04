/* Tiny history charts for the telemetry panels. */

function prepare(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 80;
  const height = canvas.clientHeight || 20;
  if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

/**
 * @param {HTMLCanvasElement} canvas
 * @param {Array<{t: number, v: number}>} samples oldest first
 */
export function drawSparkline(canvas, samples, options = {}) {
  const {
    colour = '#4f7fce',
    fill = true,
    min: forcedMin,
    max: forcedMax,
    zeroFloor = false,
  } = options;

  const { ctx, width, height } = prepare(canvas);
  const points = (samples || []).filter((s) => Number.isFinite(s.v));
  if (points.length < 2) {
    ctx.fillStyle = 'rgba(128,140,165,0.35)';
    ctx.fillRect(0, height - 1, width, 1);
    return;
  }

  let min = forcedMin ?? Math.min(...points.map((p) => p.v));
  let max = forcedMax ?? Math.max(...points.map((p) => p.v));
  if (zeroFloor && min > 0) min = 0;
  if (max - min < 1e-9) {
    // A flat line still deserves to be visible, centred.
    max += 0.5;
    min -= 0.5;
  }

  const tMin = points[0].t;
  const tMax = points[points.length - 1].t;
  const tSpan = Math.max(tMax - tMin, 1e-6);
  const pad = 1.5;
  const toX = (t) => ((t - tMin) / tSpan) * width;
  const toY = (v) => height - pad - ((v - min) / (max - min)) * (height - pad * 2);

  ctx.beginPath();
  points.forEach((point, index) => {
    const x = toX(point.t);
    const y = toY(point.v);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });

  if (fill) {
    ctx.save();
    ctx.lineTo(toX(tMax), height);
    ctx.lineTo(toX(tMin), height);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, `${colour}44`);
    gradient.addColorStop(1, `${colour}05`);
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.restore();

    ctx.beginPath();
    points.forEach((point, index) => {
      const x = toX(point.t);
      const y = toY(point.v);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
  }

  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.4;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  // Mark the latest reading so the eye lands on "now".
  const last = points[points.length - 1];
  ctx.beginPath();
  ctx.arc(toX(last.t), toY(last.v), 1.9, 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.fill();
}

/**
 * A spirit-level style attitude indicator: the bubble sits where the hull is
 * tilted to, which is faster to read at a glance than two numbers.
 */
export function drawLevelBubble(canvas, { roll = 0, pitch = 0, limit = 12, label } = {}) {
  const { ctx, width, height } = prepare(canvas);
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) / 2 - 3;
  if (radius <= 2) return;

  const styles = getComputedStyle(document.documentElement);
  const line = styles.getPropertyValue('--line').trim() || '#e3e8f2';
  const accent = styles.getPropertyValue('--brand-500').trim() || '#3a62b4';
  const muted = styles.getPropertyValue('--muted').trim() || '#5d6a86';

  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  for (const fraction of [1, 0.5]) {
    ctx.beginPath();
    ctx.arc(cx, cy, radius * fraction, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(cx - radius, cy);
  ctx.lineTo(cx + radius, cy);
  ctx.moveTo(cx, cy - radius);
  ctx.lineTo(cx, cy + radius);
  ctx.stroke();

  const clamp = (value) => Math.max(-1, Math.min(1, value / limit));
  const bx = cx + clamp(roll) * radius;
  const by = cy - clamp(pitch) * radius;
  const magnitude = Math.hypot(roll, pitch);
  const colour = magnitude > limit * 0.7 ? '#c62b32' : magnitude > limit * 0.4 ? '#b7791f' : accent;

  ctx.beginPath();
  ctx.arc(bx, by, 5.5, 0, Math.PI * 2);
  ctx.fillStyle = `${colour}33`;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(bx, by, 3, 0, Math.PI * 2);
  ctx.fillStyle = colour;
  ctx.fill();

  if (label) {
    ctx.fillStyle = muted;
    ctx.font = '600 9px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(label, cx, height - 1);
  }
}

/** Compass rose showing heading, with an optional course-over-ground needle. */
export function drawCompass(canvas, { heading = 0, course = null } = {}) {
  const { ctx, width, height } = prepare(canvas);
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) / 2 - 8;
  if (radius <= 4) return;

  const styles = getComputedStyle(document.documentElement);
  const line = styles.getPropertyValue('--line').trim() || '#e3e8f2';
  const ink = styles.getPropertyValue('--ink').trim() || '#101a34';
  const muted = styles.getPropertyValue('--muted').trim() || '#5d6a86';
  const accent = styles.getPropertyValue('--brand').trim() || '#203a70';

  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = muted;
  ctx.font = '600 8px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (const [text, angle] of [['N', 0], ['E', 90], ['S', 180], ['W', 270]]) {
    const rad = (angle * Math.PI) / 180;
    ctx.fillText(text, cx + Math.sin(rad) * (radius + 5), cy - Math.cos(rad) * (radius + 5));
  }

  if (course !== null && Number.isFinite(course)) {
    const rad = (course * Math.PI) / 180;
    ctx.strokeStyle = muted;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.sin(rad) * radius, cy - Math.cos(rad) * radius);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  const rad = (heading * Math.PI) / 180;
  const tip = [cx + Math.sin(rad) * radius * 0.92, cy - Math.cos(rad) * radius * 0.92];
  const left = [cx + Math.sin(rad + 2.5) * radius * 0.42, cy - Math.cos(rad + 2.5) * radius * 0.42];
  const right = [cx + Math.sin(rad - 2.5) * radius * 0.42, cy - Math.cos(rad - 2.5) * radius * 0.42];
  ctx.beginPath();
  ctx.moveTo(...tip);
  ctx.lineTo(...left);
  ctx.lineTo(cx, cy);
  ctx.lineTo(...right);
  ctx.closePath();
  ctx.fillStyle = accent;
  ctx.fill();

  ctx.fillStyle = ink;
  ctx.font = '700 11px system-ui, sans-serif';
  ctx.fillText(`${Math.round(heading)}°`, cx, cy + radius * 0.62);
}
